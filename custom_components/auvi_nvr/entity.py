"""Shared entity and device helpers."""

from __future__ import annotations

import inspect
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, RECORDER_MODEL
from .coordinator import AuviCoordinator, AuviData

_VIA_DEVICE_ID: bool | None = None


def _registry_takes_via_device_id() -> bool:
    """Whether this Home Assistant's device registry has via_device_id (2026.8+)."""
    global _VIA_DEVICE_ID  # noqa: PLW0603
    if _VIA_DEVICE_ID is None:
        params = inspect.signature(dr.DeviceRegistry.async_get_or_create).parameters
        _VIA_DEVICE_ID = "via_device_id" in params
    return _VIA_DEVICE_ID


def camera_identifier(uid: str, camera_id: str) -> tuple[str, str]:
    """The registry identifier of a camera device."""
    return (DOMAIN, f"{uid}_{camera_id}")


@callback
def ensure_camera_devices(
    hass: HomeAssistant, entry: ConfigEntry, uid: str, hub_device_id: str, data: AuviData
) -> None:
    """Create or refresh one device per camera, linked to the recorder.

    Done from the integration's own code rather than through the entities'
    DeviceInfo: the link to the recorder uses `via_device_id` where the
    registry has it and the deprecated `via_device` elsewhere, and Home
    Assistant only tolerates the deprecated form when the call is
    attributable to a custom integration frame.
    """
    registry = dr.async_get(hass)
    link: dict[str, Any] = (
        {"via_device_id": hub_device_id}
        if _registry_takes_via_device_id()
        else {"via_device": (DOMAIN, uid)}
    )
    for camera_id, cam in data.cameras.items():
        info = data.infos.get(camera_id) or {}
        kwargs: dict[str, Any] = {
            "name": cam.get("name") or info.get("name") or camera_id,
            "manufacturer": info.get("make") or None,
            "model": info.get("model") or None,
            "sw_version": info.get("firmware") or None,
            "serial_number": info.get("serial") or None,
            "configuration_url": info.get("web_admin_url") or None,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={camera_identifier(uid, camera_id)},
            **kwargs,
            **link,
        )


def recorder_device_info(uid: str, data: AuviData, host: str, port: int, title: str) -> DeviceInfo:
    """DeviceInfo for the recorder itself."""
    return DeviceInfo(
        identifiers={(DOMAIN, uid)},
        manufacturer=MANUFACTURER,
        model=RECORDER_MODEL,
        name=title,
        sw_version=data.agent.get("version"),
        configuration_url=f"https://{host}:{port}/local/",
    )


def camera_device_info(uid: str, hub_device_id: str, data: AuviData, camera_id: str) -> DeviceInfo:
    """DeviceInfo for one camera: identifiers only, the device itself is
    created by ensure_camera_devices (which owns the recorder link)."""
    cam = data.cameras.get(camera_id) or {}
    info = data.infos.get(camera_id) or {}
    return DeviceInfo(
        identifiers={camera_identifier(uid, camera_id)},
        name=cam.get("name") or info.get("name") or camera_id,
    )


class AuviRecorderEntity(CoordinatorEntity[AuviCoordinator]):
    """Base for entities on the recorder device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AuviCoordinator, uid: str, key: str, device: DeviceInfo) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{uid}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device


class AuviCameraEntity(CoordinatorEntity[AuviCoordinator]):
    """Base for entities on one camera's device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AuviCoordinator, uid: str, camera_id: str, key: str, device: DeviceInfo
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.camera_id = camera_id
        self._attr_unique_id = f"{uid}_{camera_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device

    @property
    def available(self) -> bool:
        """Unavailable when the recorder stops reporting the camera."""
        return super().available and (
            self.camera_id in self.coordinator.data.cameras
            or self.coordinator.data.camera_health(self.camera_id) is not None
        )
