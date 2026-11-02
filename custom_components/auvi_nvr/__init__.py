"""Auvi NVR: cameras, recorder status and the recorder's own viewer page."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import ssl

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuviAuthError, AuviClient, AuviConnectionError, build_ssl_context
from .const import (
    CONF_CA_PEM,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    RECORDER_MODEL,
)
from .coordinator import AuviCoordinator
from .entity import ensure_camera_devices
from .panel import PanelSession, async_maybe_remove_panel, async_setup_panel

LOGGER = logging.getLogger(__package__)


@dataclass
class AuviRuntimeData:
    """Everything a loaded entry owns."""

    client: AuviClient
    coordinator: AuviCoordinator
    uid: str
    hub_device_id: str
    panel_sessions: dict[str, PanelSession] = field(default_factory=dict)


type AuviConfigEntry = ConfigEntry[AuviRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: AuviConfigEntry) -> bool:
    """Log in to the recorder, start polling, register the panel."""
    try:
        ctx = await hass.async_add_executor_job(build_ssl_context, entry.data[CONF_CA_PEM])
    except (ssl.SSLError, ValueError) as err:
        raise ConfigEntryNotReady(f"stored recorder certificate is unusable: {err}") from err

    host: str = entry.data[CONF_HOST]
    port: int = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
    client = AuviClient(
        async_get_clientsession(hass),
        host,
        port,
        entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        entry.data[CONF_PASSWORD],
        ctx,
    )
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
    entry.runtime_data = AuviRuntimeData(
        client=client, coordinator=coordinator, uid=uid, hub_device_id=hub.id
    )

    # Camera devices are created here (and refreshed on every poll) so the
    # link to the recorder is made from the integration's own code.
    ensure_camera_devices(hass, entry, uid, hub.id, coordinator.data)
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: ensure_camera_devices(hass, entry, uid, hub.id, coordinator.data)
        )
    )

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
