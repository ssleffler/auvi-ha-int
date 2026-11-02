"""Binary sensors: camera connectivity and recording, recorder links, live detections."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import CONF_HOST, CONF_PORT, EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import AuviConfigEntry
from .const import (
    CONF_EVENT_HOLD,
    DEFAULT_EVENT_HOLD,
    DEFAULT_PORT,
    SIGNAL_EVENT,
    TRIGGER_ACTIVITY,
)
from .coordinator import AuviCoordinator, AuviData
from .entity import AuviCameraEntity, AuviRecorderEntity, camera_device_info, recorder_device_info
from .events import AuviEvent


@dataclass(frozen=True, kw_only=True)
class AuviRecorderBinaryDescription(BinarySensorEntityDescription):
    """A recorder-level yes/no."""

    value_fn: Callable[[AuviData], bool | None]


@dataclass(frozen=True, kw_only=True)
class AuviCameraBinaryDescription(BinarySensorEntityDescription):
    """A per-camera yes/no."""

    value_fn: Callable[[AuviData, str], bool | None]


@dataclass(frozen=True, kw_only=True)
class AuviDetectionDescription(BinarySensorEntityDescription):
    """A per-camera detection that switches on for a hold after each event."""

    # The recorder event types that switch this sensor on.
    event_types: frozenset[str] | None = None  # None = any object or analytic event


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

DETECTION_DESCRIPTIONS: tuple[AuviDetectionDescription, ...] = (
    AuviDetectionDescription(
        key=TRIGGER_ACTIVITY,
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        event_types=None,
    ),
    AuviDetectionDescription(
        key="person",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        event_types=frozenset({"person"}),
    ),
    AuviDetectionDescription(
        key="vehicle",
        icon="mdi:car",
        event_types=frozenset({"vehicle"}),
    ),
    AuviDetectionDescription(
        key="animal",
        icon="mdi:paw",
        event_types=frozenset({"animal"}),
    ),
    AuviDetectionDescription(
        key="license_plate",
        icon="mdi:card-text",
        event_types=frozenset({"license_plate"}),
    ),
    AuviDetectionDescription(
        key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        event_types=frozenset({"motion"}),
    ),
    AuviDetectionDescription(
        key="face",
        icon="mdi:face-recognition",
        entity_registry_enabled_default=False,
        event_types=frozenset({"face"}),
    ),
    AuviDetectionDescription(
        key="line_cross",
        icon="mdi:vector-line",
        entity_registry_enabled_default=False,
        event_types=frozenset({"line_cross"}),
    ),
    AuviDetectionDescription(
        key="loiter",
        icon="mdi:timer-sand",
        entity_registry_enabled_default=False,
        event_types=frozenset({"loiter"}),
    ),
    AuviDetectionDescription(
        key="intrusion",
        icon="mdi:shield-alert",
        entity_registry_enabled_default=False,
        event_types=frozenset({"intrusion"}),
    ),
    AuviDetectionDescription(
        key="tamper",
        device_class=BinarySensorDeviceClass.TAMPER,
        entity_registry_enabled_default=False,
        event_types=frozenset({"tamper", "view_change"}),
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
    hold = int(entry.options.get(CONF_EVENT_HOLD, DEFAULT_EVENT_HOLD))
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new: list[BinarySensorEntity] = []
        for cam_id in coordinator.data.cameras:
            if cam_id in known:
                continue
            known.add(cam_id)
            device = camera_device_info(rt.uid, rt.hub_device_id, coordinator.data, cam_id)
            new.extend(
                AuviCameraBinarySensor(coordinator, rt.uid, cam_id, desc, device)
                for desc in CAMERA_DESCRIPTIONS
            )
            new.extend(
                AuviDetectionSensor(coordinator, rt.uid, cam_id, desc, device, entry.entry_id, hold)
                for desc in DETECTION_DESCRIPTIONS
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


class AuviDetectionSensor(AuviCameraEntity, BinarySensorEntity):
    """On for a hold after each matching event the recorder posts."""

    entity_description: AuviDetectionDescription

    def __init__(
        self, coordinator: AuviCoordinator, uid: str, camera_id: str,
        desc: AuviDetectionDescription, device, entry_id: str, hold: int,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator, uid, camera_id, desc.key, device)
        self.entity_description = desc
        self._entry_id = entry_id
        self._hold = hold
        self._attr_is_on = False
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._unsub_off: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Listen for the recorder's events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_EVENT.format(self._entry_id), self._on_event
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending switch-off."""
        if self._unsub_off is not None:
            self._unsub_off()
            self._unsub_off = None
        await super().async_will_remove_from_hass()

    def _matches(self, event: AuviEvent) -> bool:
        types = self.entity_description.event_types
        if types is None:
            return event.is_activity
        return event.event_type in types

    @callback
    def _on_event(self, event: AuviEvent) -> None:
        if event.camera_id != self.camera_id or not self._matches(event):
            return
        if self._unsub_off is not None:
            self._unsub_off()
        self._attr_is_on = True
        self._attr_extra_state_attributes = event.attributes()
        self._unsub_off = async_call_later(self.hass, self._hold, self._switch_off)
        self.async_write_ha_state()

    @callback
    def _switch_off(self, _now: Any) -> None:
        self._unsub_off = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Whether a matching event arrived within the hold."""
        return self._attr_is_on
