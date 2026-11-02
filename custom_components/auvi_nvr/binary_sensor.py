"""Binary sensors: camera connectivity and recording, recorder links."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import CONF_HOST, CONF_PORT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AuviConfigEntry
from .const import DEFAULT_PORT
from .coordinator import AuviCoordinator, AuviData
from .entity import AuviCameraEntity, AuviRecorderEntity, camera_device_info, recorder_device_info


@dataclass(frozen=True, kw_only=True)
class AuviRecorderBinaryDescription(BinarySensorEntityDescription):
    """A recorder-level yes/no."""

    value_fn: Callable[[AuviData], bool | None]


@dataclass(frozen=True, kw_only=True)
class AuviCameraBinaryDescription(BinarySensorEntityDescription):
    """A per-camera yes/no."""

    value_fn: Callable[[AuviData, str], bool | None]


RECORDER_DESCRIPTIONS: tuple[AuviRecorderBinaryDescription, ...] = (
    AuviRecorderBinaryDescription(
        key="cloud_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool((d.agent.get("control") or {}).get("connected")),
    ),
    AuviRecorderBinaryDescription(
        key="rtsp_listening",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.rtsp.get("enabled") and d.rtsp.get("listening")),
    ),
)

CAMERA_DESCRIPTIONS: tuple[AuviCameraBinaryDescription, ...] = (
    AuviCameraBinaryDescription(
        key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda d, cam: d.camera_online(cam),
    ),
    AuviCameraBinaryDescription(
        key="recording",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d, cam: d.camera_recording(cam),
    ),
    AuviCameraBinaryDescription(
        key="rtsp_shared",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, cam: bool(d.shared_classes(cam)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AuviConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add recorder sensors now and camera sensors as cameras appear."""
    rt = entry.runtime_data
    coordinator = rt.coordinator
    recorder = recorder_device_info(
        rt.uid, coordinator.data, entry.data[CONF_HOST],
        int(entry.data.get(CONF_PORT, DEFAULT_PORT)), entry.title,
    )
    async_add_entities(
        AuviRecorderBinarySensor(coordinator, rt.uid, desc, recorder)
        for desc in RECORDER_DESCRIPTIONS
    )
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new: list[AuviCameraBinarySensor] = []
        for cam_id in coordinator.data.cameras:
            if cam_id in known:
                continue
            known.add(cam_id)
            device = camera_device_info(rt.uid, rt.hub_device_id, coordinator.data, cam_id)
            new.extend(
                AuviCameraBinarySensor(coordinator, rt.uid, cam_id, desc, device)
                for desc in CAMERA_DESCRIPTIONS
            )
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class AuviRecorderBinarySensor(AuviRecorderEntity, BinarySensorEntity):
    """Recorder-level binary sensor."""

    entity_description: AuviRecorderBinaryDescription

    def __init__(self, coordinator: AuviCoordinator, uid: str, desc: AuviRecorderBinaryDescription, device) -> None:
        """Initialise."""
        super().__init__(coordinator, uid, desc.key, device)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)


class AuviCameraBinarySensor(AuviCameraEntity, BinarySensorEntity):
    """Per-camera binary sensor."""

    entity_description: AuviCameraBinaryDescription

    def __init__(
        self, coordinator: AuviCoordinator, uid: str, camera_id: str,
        desc: AuviCameraBinaryDescription, device,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator, uid, camera_id, desc.key, device)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data, self.camera_id)
