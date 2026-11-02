"""Sidebar panel: a custom element that frames the proxied recorder UI.

The frame's requests carry no Home Assistant bearer, so the panel first
asks Home Assistant (over its authenticated websocket) for a short-lived
signed URL to the entry view, which sets an HttpOnly session cookie scoped
to this recorder's proxy prefix and redirects into the viewer. The same
pattern Supervisor ingress uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from pathlib import Path
import secrets
import time

from aiohttp import web
import voluptuous as vol

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView, StaticPathConfig
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_PANEL_REQUIRE_ADMIN,
    COOKIE_NAME,
    DATA_HTTP_REGISTERED,
    DATA_PANEL_REGISTERED,
    DOMAIN,
    ENTER_SIGNATURE_SECONDS,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PROXY_PREFIX,
    SESSION_TTL,
    STATIC_URL,
    WS_TYPE_LIST,
    WS_TYPE_PANEL_SESSION,
)
from .proxy import AuviProxyView

LOGGER = logging.getLogger(__package__)


@dataclass
class PanelSession:
    """One browser's session with the proxied viewer."""

    sid: str
    user_id: str
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)


def _loaded_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]


async def async_setup_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register views, websocket commands and the sidebar entry (once)."""
    if not hass.data.get(DATA_HTTP_REGISTERED):
        hass.data[DATA_HTTP_REGISTERED] = True
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    f"{STATIC_URL}/auvi-panel.js",
                    str(Path(__file__).parent / "www" / "auvi-panel.js"),
                    True,
                )
            ]
        )
        # The entry view must be registered before the catch-all proxy.
        hass.http.register_view(AuviEnterView(hass))
        hass.http.register_view(AuviProxyView(hass))
        websocket_api.async_register_command(hass, ws_list)
        websocket_api.async_register_command(hass, ws_panel_session)

    if not hass.data.get(DATA_PANEL_REGISTERED):
        require_admin = bool(entry.options.get(CONF_PANEL_REQUIRE_ADMIN, False))
        try:
            frontend.async_register_built_in_panel(
                hass,
                component_name="custom",
                sidebar_title=PANEL_TITLE,
                sidebar_icon=PANEL_ICON,
                frontend_url_path=PANEL_URL_PATH,
                config={
                    "_panel_custom": {
                        "name": PANEL_ELEMENT,
                        "module_url": f"{STATIC_URL}/auvi-panel.js",
                        "embed_iframe": False,
                        "trust_external": False,
                    }
                },
                require_admin=require_admin,
            )
        except ValueError as err:
            LOGGER.debug("panel already registered: %s", err)
        hass.data[DATA_PANEL_REGISTERED] = True


@callback
def async_maybe_remove_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the sidebar entry when the last recorder unloads."""
    if any(e.entry_id != entry.entry_id for e in _loaded_entries(hass)):
        return
    if hass.data.pop(DATA_PANEL_REGISTERED, None):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST})
@callback
def ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List loaded recorders for the panel's switcher."""
    connection.send_result(
        msg["id"],
        {"entries": [{"entry_id": e.entry_id, "title": e.title} for e in _loaded_entries(hass)]},
    )


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_PANEL_SESSION, vol.Optional("entry_id"): str}
)
@callback
def ws_panel_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Open a panel session and return the signed entry URL for the frame."""
    entries = _loaded_entries(hass)
    entry = next((e for e in entries if e.entry_id == msg.get("entry_id")), None)
    if entry is None and entries and not msg.get("entry_id"):
        entry = entries[0]
    if entry is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no Auvi NVR recorder is loaded")
        return
    if entry.options.get(CONF_PANEL_REQUIRE_ADMIN) and not connection.user.is_admin:
        connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "the Auvi NVR panel is limited to administrators")
        return
    sessions = entry.runtime_data.panel_sessions
    now = time.monotonic()
    for sid in [s for s, sess in sessions.items() if now - sess.last_seen > SESSION_TTL]:
        sessions.pop(sid, None)
    sid = secrets.token_urlsafe(32)
    sessions[sid] = PanelSession(sid=sid, user_id=connection.user.id)
    enter_url = async_sign_path(
        hass,
        f"{PROXY_PREFIX}/{entry.entry_id}/enter?sid={sid}",
        timedelta(seconds=ENTER_SIGNATURE_SECONDS),
    )
    connection.send_result(
        msg["id"],
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "enter_url": enter_url,
            "base": f"{PROXY_PREFIX}/{entry.entry_id}",
            "ttl": SESSION_TTL,
        },
    )


class AuviEnterView(HomeAssistantView):
    """Turn a signed entry URL into the panel session cookie, then redirect."""

    url = PROXY_PREFIX + "/{entry_id}/enter"
    name = "api:auvi_nvr:enter"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        """Validate the session against the signed-in user and set the cookie."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return web.Response(status=404, text="unknown or unloaded recorder")
        sid = request.query.get("sid", "")
        sess = entry.runtime_data.panel_sessions.get(sid)
        user = request.get(KEY_HASS_USER)
        if sess is None or user is None or user.id != sess.user_id:
            return web.Response(status=403, text="this panel session is not yours or has expired")
        prefix = f"{PROXY_PREFIX}/{entry_id}"
        resp = web.Response(
            status=302, headers={"Location": f"{prefix}/local/", "Cache-Control": "no-store"}
        )
        resp.set_cookie(
            COOKIE_NAME,
            sid,
            path=prefix + "/",
            httponly=True,
            samesite="Lax",
            secure=request.url.scheme == "https",
            max_age=SESSION_TTL,
        )
        return resp
