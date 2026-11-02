"""Diagnostics with every secret redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import AuviConfigEntry
from .const import CONF_CA_PEM, CONF_RTSP_PASSWORD

TO_REDACT = {CONF_PASSWORD, CONF_RTSP_PASSWORD, CONF_CA_PEM, CONF_HOST, "token", "addresses",
             "host", "web_admin_url", "rtsp", "serial", "url_hint", "address_hint", "snapshot_hint"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AuviConfigEntry
) -> dict[str, Any]:
    """Return the entry and the last poll, redacted."""
    rt = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "data": async_redact_data(asdict(rt.coordinator.data), TO_REDACT),
        "panel_sessions": len(rt.panel_sessions),
        "last_update_success": rt.coordinator.last_update_success,
    }
