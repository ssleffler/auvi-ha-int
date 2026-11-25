"""Renamed cameras keep their devices and entities (recorder 26.11 UUID ids)."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.auvi_nvr.const import DOMAIN
from custom_components.auvi_nvr.coordinator import AuviData
from custom_components.auvi_nvr.entity import migrate_renamed_cameras, renamed_cameras

UID, OLD, NEW = "rec", "rlc-823a-13", "0b0e7c1e-4f2a-4d6b-9a55-2f7f7f1f3c11"


async def test_renamed_camera_keeps_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    old_dev = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{UID}_{OLD}")}
    )
    kept = entities.async_get_or_create(
        "camera", DOMAIN, f"{UID}_{OLD}_camera", config_entry=entry, device_id=old_dev.id,
        suggested_object_id="front_door",
    )
    # The duplicate the previous version made under the new id.
    dup_dev = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{UID}_{NEW}")}
    )
    dup = entities.async_get_or_create(
        "camera", DOMAIN, f"{UID}_{NEW}_camera", config_entry=entry, device_id=dup_dev.id,
        suggested_object_id="front_door",
    )
    data = AuviData(health={"cameras": [
        {"camera_id": NEW, "legacy_camera_id": OLD},
        {"camera_id": "never-renamed"},
    ]})

    assert migrate_renamed_cameras(hass, UID, data) == 1

    assert entities.async_get(dup.entity_id) is None
    assert devices.async_get(dup_dev.id) is None
    assert entities.async_get(kept.entity_id).unique_id == f"{UID}_{NEW}_camera"
    assert devices.async_get(old_dev.id).identifiers == {(DOMAIN, f"{UID}_{NEW}")}
    assert renamed_cameras(hass, UID, data) == []
