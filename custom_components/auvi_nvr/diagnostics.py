"""Diagnostics with every secret redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant

from . import AuviConfigEntry
from .const import CONF_CA_PEM, CONF_EVENT_SECRET, CONF_EVENTS_BASE_URL, CONF_RTSP_PASSWORD

TO_REDACT = {CONF_PASSWORD, CONF_RTSP_PASSWORD, CONF_CA_PEM, CONF_HOST, CONF_EVENT_SECRET,
             CONF_WEBHOOK_ID, CONF_EVENTS_BASE_URL, "token", "addresses", "host",
             "web_admin_url", "rtsp", "serial", "url_hint", "address_hint", "snapshot_hint"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AuviConfigEntry
) -> dict[str, Any]:
    """Return the entry, the last poll and the event feed's state, redacted."""
    rt = entry.runtime_data
    events = rt.events
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "data": async_redact_data(asdict(rt.coordinator.data), TO_REDACT),
        "panel_sessions": len(rt.panel_sessions),
        "last_update_success": rt.coordinator.last_update_success,
        "events": {
            "enabled": events.enabled,
            "received": events.received,
            "rejected": events.rejected,
            "rules_error": events.rules_error,
            "rules": events.rules_synced,
            "last": {
                cam: {"type": ev.event_type, "time": ev.when.isoformat()}
                for cam, ev in events.last.items()
            },
        },
    }
