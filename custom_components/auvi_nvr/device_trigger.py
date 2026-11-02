"""Device triggers: "Person detected on Front Door" in the automation editor.

Each trigger listens for the integration's ``auvi_nvr_event`` on the event
bus, filtered by the camera's device id and the event type.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_KINDS, EVENT_TYPE, TRIGGER_ACTIVITY

TRIGGER_TYPES: list[str] = [TRIGGER_ACTIVITY, *EVENT_KINDS]

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _is_camera_device(hass: HomeAssistant, device_id: str) -> bool:
    """Whether the device is one of our cameras (not the recorder itself)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return False
    hubs = {
        entry.unique_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.unique_id
    }
    for domain, ident in device.identifiers:
        if domain == DOMAIN and ident not in hubs and "_" in ident:
            return True
    return False


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    """List the triggers a camera device offers."""
    if not _is_camera_device(hass, device_id):
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach: an event trigger on auvi_nvr_event for this device and type."""
    event_data: dict[str, Any] = {CONF_DEVICE_ID: config[CONF_DEVICE_ID]}
    if config[CONF_TYPE] == TRIGGER_ACTIVITY:
        event_data["activity"] = True
    else:
        event_data["type"] = config[CONF_TYPE]
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_TYPE,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
