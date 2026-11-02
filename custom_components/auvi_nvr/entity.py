"""Shared entity and device helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, RECORDER_MODEL
from .coordinator import AuviCoordinator, AuviData

def _via_kwargs(hub_device_id: str, uid: str) -> dict[str, Any]:
    """Link a camera device to the recorder.

    `via_device` is accepted by every supported release (deprecated with a
    warning for custom integrations from 2026.8, removed 2027.8); the
    `via_device_id` replacement is adopted once the minimum HA version
    carries it. hub_device_id is kept for that switch.
    """
    return {"via_device": (DOMAIN, uid)}


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


def camera_device_info(
    uid: str, hub_device_id: str, data: AuviData, camera_id: str
) -> DeviceInfo:
    """DeviceInfo for one camera, linked to the recorder."""
    cam = data.cameras.get(camera_id) or {}
    info = data.infos.get(camera_id) or {}
    kwargs: dict[str, Any] = {
        "identifiers": {(DOMAIN, f"{uid}_{camera_id}")},
        "name": cam.get("name") or info.get("name") or camera_id,
        "manufacturer": info.get("make") or None,
        "model": info.get("model") or None,
        "sw_version": info.get("firmware") or None,
        "serial_number": info.get("serial") or None,
        "configuration_url": info.get("web_admin_url") or None,
    }
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    kwargs.update(_via_kwargs(hub_device_id, uid))
    return DeviceInfo(**kwargs)


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
