"""Auvi NVR: cameras, recorder status, real-time events and the recorder's own viewer page."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import secrets
import ssl

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_WEBHOOK_ID,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuviAuthError, AuviClient, AuviConnectionError, build_ssl_context
from .const import (
    CONF_CA_PEM,
    CONF_EVENT_SECRET,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    RECORDER_MODEL,
)
from .coordinator import AuviCoordinator
from .entity import ensure_camera_devices
from .events import AuviEventBridge
from .panel import PanelSession, async_maybe_remove_panel, async_setup_panel

LOGGER = logging.getLogger(__package__)


@dataclass
class AuviRuntimeData:
    """Everything a loaded entry owns."""

    client: AuviClient
    coordinator: AuviCoordinator
    uid: str
    hub_device_id: str
    events: AuviEventBridge
    panel_sessions: dict[str, PanelSession] = field(default_factory=dict)


type AuviConfigEntry = ConfigEntry[AuviRuntimeData]


def _ensure_event_identity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give the entry its webhook id and event secret once (entries made before 0.2)."""
    if entry.data.get(CONF_WEBHOOK_ID) and entry.data.get(CONF_EVENT_SECRET):
        return
    data = dict(entry.data)
    data.setdefault(CONF_WEBHOOK_ID, webhook.async_generate_id())
    data.setdefault(CONF_EVENT_SECRET, secrets.token_urlsafe(32))
    hass.config_entries.async_update_entry(entry, data=data)


def _build_client(hass: HomeAssistant, entry: ConfigEntry, ctx: ssl.SSLContext) -> AuviClient:
    return AuviClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
        entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        entry.data[CONF_PASSWORD],
        ctx,
    )


async def async_setup_entry(hass: HomeAssistant, entry: AuviConfigEntry) -> bool:
    """Log in to the recorder, start polling, register the webhook and the panel."""
    try:
        ctx = await hass.async_add_executor_job(build_ssl_context, entry.data[CONF_CA_PEM])
    except (ssl.SSLError, ValueError) as err:
        raise ConfigEntryNotReady(f"stored recorder certificate is unusable: {err}") from err

    _ensure_event_identity(hass, entry)
    host: str = entry.data[CONF_HOST]
    port: int = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
    client = _build_client(hass, entry, ctx)
    try:
        await client.login()
    except AuviAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AuviConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = AuviCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    uid = entry.unique_id or f"{host}:{port}"
    agent = coordinator.data.agent
    hostname = coordinator.data.tls.get("hostname") or host
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, uid)},
        manufacturer=MANUFACTURER,
        model=RECORDER_MODEL,
        name=entry.title or f"Auvi NVR {hostname}",
        sw_version=agent.get("version"),
        configuration_url=f"https://{host}:{port}/local/",
    )
    events = AuviEventBridge(hass, entry, client, uid)
    entry.runtime_data = AuviRuntimeData(
        client=client, coordinator=coordinator, uid=uid, hub_device_id=hub.id, events=events
    )

    # Camera devices are created here (and refreshed on every poll) so the
    # link to the recorder is made from the integration's own code.
    ensure_camera_devices(hass, entry, uid, hub.id, coordinator.data)

    # The webhook first, so the rules we install have something to post to;
    # the rules follow the camera list on every poll.
    events.async_start()
    entry.async_on_unload(events.async_stop)
    # Not awaited: N+2 recorder calls must not hold up setup on a slow recorder.
    entry.async_create_background_task(
        hass, events.async_sync_rules(coordinator.data), f"{DOMAIN} rule sync"
    )

    @callback
    def _on_poll() -> None:
        ensure_camera_devices(hass, entry, uid, hub.id, coordinator.data)
        entry.async_create_background_task(
            hass, events.async_sync_rules(coordinator.data), f"{DOMAIN} rule sync"
        )

    entry.async_on_unload(coordinator.async_add_listener(_on_poll))

    await async_setup_panel(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AuviConfigEntry) -> bool:
    """Stop polling; drop the panel when no recorder is left."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry.runtime_data.panel_sessions.clear()
        async_maybe_remove_panel(hass, entry)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take our event rules and secret off the recorder (best effort)."""
    if not (entry.data.get(CONF_WEBHOOK_ID) and entry.data.get(CONF_EVENT_SECRET)):
        return
    try:
        ctx = await hass.async_add_executor_job(build_ssl_context, entry.data[CONF_CA_PEM])
        client = _build_client(hass, entry, ctx)
        await client.login()
    except (ssl.SSLError, ValueError, AuviAuthError, AuviConnectionError) as err:
        LOGGER.warning(
            "Could not reach %s to remove its event rules (remove them on its Rules tab): %s",
            entry.title, err,
        )
        return
    uid = entry.unique_id or f"{entry.data[CONF_HOST]}:{entry.data.get(CONF_PORT, DEFAULT_PORT)}"
    await AuviEventBridge(hass, entry, client, uid).async_remove_from_recorder()
