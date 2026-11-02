"""Sensors: recorder status, storage, uptime, version; camera status, rates, last event."""

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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AuviConfigEntry
from .const import DEFAULT_PORT, DEFAULT_STREAM_CLASS, RECORDER_STATUSES, SIGNAL_EVENT
from .coordinator import AuviCoordinator, AuviData
from .entity import AuviCameraEntity, AuviRecorderEntity, camera_device_info, recorder_device_info
from .events import AuviEvent, AuviEventBridge


@dataclass(frozen=True, kw_only=True)
class AuviRecorderSensorDescription(SensorEntityDescription):
    """A recorder-level value."""

    value_fn: Callable[[AuviData], Any]


@dataclass(frozen=True, kw_only=True)
class AuviCameraSensorDescription(SensorEntityDescription):
    """A per-camera value."""

    value_fn: Callable[[AuviData, str], Any]


@dataclass(frozen=True, kw_only=True)
class AuviEventSensorDescription(SensorEntityDescription):
    """A per-camera value taken from the last event the recorder posted."""

    # Returns (state, attributes) for an event, or None when the event
    # does not concern this sensor (the previous value stays).
    event_fn: Callable[[AuviEvent], tuple[Any, dict[str, Any]] | None]


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


def _last_event(event: AuviEvent) -> tuple[Any, dict[str, Any]]:
    return event.event_type, event.attributes()


def _last_plate(event: AuviEvent) -> tuple[Any, dict[str, Any]] | None:
    plate = event.plate
    if not plate:
        return None
    attrs = event.attributes()
    attrs.pop("plate", None)
    return plate, attrs


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

EVENT_DESCRIPTIONS: tuple[AuviEventSensorDescription, ...] = (
    AuviEventSensorDescription(
        key="last_event",
        icon="mdi:motion-sensor",
        event_fn=_last_event,
    ),
    AuviEventSensorDescription(
        key="last_plate",
        icon="mdi:card-text",
        event_fn=_last_plate,
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
        new: list[SensorEntity] = []
        for cam_id in coordinator.data.cameras:
            if cam_id in known:
                continue
            known.add(cam_id)
            device = camera_device_info(rt.uid, rt.hub_device_id, coordinator.data, cam_id)
            new.extend(
                AuviCameraSensor(coordinator, rt.uid, cam_id, desc, device)
                for desc in CAMERA_DESCRIPTIONS
            )
            new.extend(
                AuviEventSensor(coordinator, rt.uid, cam_id, desc, device, entry.entry_id, rt.events)
                for desc in EVENT_DESCRIPTIONS
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


class AuviEventSensor(AuviCameraEntity, SensorEntity):
    """Per-camera value from the last event the recorder posted."""

    entity_description: AuviEventSensorDescription

    def __init__(
        self, coordinator: AuviCoordinator, uid: str, camera_id: str,
        desc: AuviEventSensorDescription, device, entry_id: str, events: AuviEventBridge,
    ) -> None:
        """Initialise; seeds from an event received earlier in this run."""
        super().__init__(coordinator, uid, camera_id, desc.key, device)
        self.entity_description = desc
        self._entry_id = entry_id
        self._attr_native_value: Any = None
        self._attr_extra_state_attributes: dict[str, Any] = {}
        if (last := events.last.get(camera_id)) is not None:
            self._apply(last)

    async def async_added_to_hass(self) -> None:
        """Listen for the recorder's events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_EVENT.format(self._entry_id), self._on_event
            )
        )

    def _apply(self, event: AuviEvent) -> bool:
        result = self.entity_description.event_fn(event)
        if result is None:
            return False
        self._attr_native_value, self._attr_extra_state_attributes = result
        return True

    @callback
    def _on_event(self, event: AuviEvent) -> None:
        if event.camera_id == self.camera_id and self._apply(event):
            self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        """The last matching event's value."""
        return self._attr_native_value
