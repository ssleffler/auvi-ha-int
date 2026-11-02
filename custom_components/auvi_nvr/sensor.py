"""Sensors: recorder status, storage, uptime, version; camera status and rates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AuviConfigEntry
from .const import DEFAULT_PORT, DEFAULT_STREAM_CLASS, RECORDER_STATUSES
from .coordinator import AuviCoordinator, AuviData
from .entity import AuviCameraEntity, AuviRecorderEntity, camera_device_info, recorder_device_info


@dataclass(frozen=True, kw_only=True)
class AuviRecorderSensorDescription(SensorEntityDescription):
    """A recorder-level value."""

    value_fn: Callable[[AuviData], Any]


@dataclass(frozen=True, kw_only=True)
class AuviCameraSensorDescription(SensorEntityDescription):
    """A per-camera value."""

    value_fn: Callable[[AuviData, str], Any]


def _status(value: Any) -> str | None:
    return value if value in RECORDER_STATUSES else None


def _projected_hours(d: AuviData) -> float | None:
    hours = (d.agent.get("disk") or {}).get("projected_hours_to_floor")
    if hours is None or hours < 0:
        return None
    return float(hours)


def _first_stream(d: AuviData, cam: str) -> dict[str, Any]:
    return (
        d.stream_health(cam, DEFAULT_STREAM_CLASS)
        or d.stream_health(cam, "main")
        or {}
    )


RECORDER_DESCRIPTIONS: tuple[AuviRecorderSensorDescription, ...] = (
    AuviRecorderSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=RECORDER_STATUSES,
        value_fn=lambda d: _status(d.health.get("status")),
    ),
    AuviRecorderSensorDescription(
        key="disk_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: (d.agent.get("disk") or {}).get("free_bytes"),
    ),
    AuviRecorderSensorDescription(
        key="projected_hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_projected_hours,
    ),
    AuviRecorderSensorDescription(
        key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.agent.get("uptime_s"),
    ),
    AuviRecorderSensorDescription(
        key="version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.agent.get("version"),
    ),
    AuviRecorderSensorDescription(
        key="camera_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: len(d.cameras),
    ),
)

CAMERA_DESCRIPTIONS: tuple[AuviCameraSensorDescription, ...] = (
    AuviCameraSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=RECORDER_STATUSES,
        value_fn=lambda d, cam: _status((d.camera_health(cam) or {}).get("status")),
    ),
    AuviCameraSensorDescription(
        key="fps",
        native_unit_of_measurement="fps",
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, cam: _first_stream(d, cam).get("fps"),
    ),
    AuviCameraSensorDescription(
        key="bitrate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBITS_PER_SECOND,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, cam: _first_stream(d, cam).get("bitrate_kbps"),
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
        AuviRecorderSensor(coordinator, rt.uid, desc, recorder) for desc in RECORDER_DESCRIPTIONS
    )
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new: list[AuviCameraSensor] = []
        for cam_id in coordinator.data.cameras:
            if cam_id in known:
                continue
            known.add(cam_id)
            device = camera_device_info(rt.uid, rt.hub_device_id, coordinator.data, cam_id)
            new.extend(
                AuviCameraSensor(coordinator, rt.uid, cam_id, desc, device)
                for desc in CAMERA_DESCRIPTIONS
            )
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class AuviRecorderSensor(AuviRecorderEntity, SensorEntity):
    """Recorder-level sensor."""

    entity_description: AuviRecorderSensorDescription

    def __init__(self, coordinator: AuviCoordinator, uid: str, desc: AuviRecorderSensorDescription, device) -> None:
        """Initialise."""
        super().__init__(coordinator, uid, desc.key, device)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)


class AuviCameraSensor(AuviCameraEntity, SensorEntity):
    """Per-camera sensor."""

    entity_description: AuviCameraSensorDescription

    def __init__(
        self, coordinator: AuviCoordinator, uid: str, camera_id: str,
        desc: AuviCameraSensorDescription, device,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator, uid, camera_id, desc.key, device)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data, self.camera_id)
