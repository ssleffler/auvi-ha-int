"""Camera entities for streams the recorder shares over RTSP passthrough."""

from __future__ import annotations

import logging
from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AuviConfigEntry
from .const import CONF_RTSP_PASSWORD, CONF_STREAM_CLASS, DEFAULT_STREAM_CLASS
from .coordinator import AuviCoordinator
from .entity import AuviCameraEntity, camera_device_info

LOGGER = logging.getLogger(__package__)


def _eligible(entry: AuviConfigEntry) -> list[str]:
    """Cameras with at least one shared class, when an RTSP password exists."""
    if not entry.options.get(CONF_RTSP_PASSWORD):
        return []
    data = entry.runtime_data.coordinator.data
    return [
        cam_id for cam_id in data.cameras
        if data.shared_classes(cam_id)
    ]


async def async_setup_entry(
    hass: HomeAssistant, entry: AuviConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add a camera entity per shared camera, now and as sharing changes."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new = [
            AuviCamera(coordinator, entry, cam_id)
            for cam_id in _eligible(entry)
            if cam_id not in known
        ]
        for cam in new:
            known.add(cam.camera_id)
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class AuviCamera(AuviCameraEntity, Camera):
    """One shared stream: RTSP over TCP for video, the RTSP port's still."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: AuviCoordinator, entry: AuviConfigEntry, camera_id: str) -> None:
        """Initialise."""
        rt = entry.runtime_data
        AuviCameraEntity.__init__(
            self, coordinator, rt.uid, camera_id, "camera",
            camera_device_info(rt.uid, rt.hub_device_id, coordinator.data, camera_id),
        )
        Camera.__init__(self)
        self._entry = entry
        # The recorder re-serves the camera's own bytes; TCP keeps the
        # packets in order through Home Assistant's stream worker.
        self.stream_options["rtsp_transport"] = "tcp"
        info = coordinator.data.infos.get(camera_id) or {}
        self._attr_brand = info.get("make") or None
        self._attr_model = info.get("model") or None

    @property
    def stream_class(self) -> str | None:
        """The shared class to use: the configured one, else whatever is shared."""
        shared = self.coordinator.data.shared_classes(self.camera_id)
        if not shared:
            return None
        want = self._entry.options.get(CONF_STREAM_CLASS, DEFAULT_STREAM_CLASS)
        return want if want in shared else shared[0]

    @property
    def available(self) -> bool:
        """Unavailable when the stream is no longer shared."""
        return super().available and self.stream_class is not None

    @property
    def is_streaming(self) -> bool:
        """Delivering frames to the recorder."""
        return bool(self.coordinator.data.camera_online(self.camera_id))

    @property
    def is_recording(self) -> bool:
        """Writing segments on the recorder."""
        return bool(self.coordinator.data.camera_recording(self.camera_id))

    async def stream_source(self) -> str | None:
        """rtsp://user:pass@recorder:8554/<camera>/<class>."""
        cls = self.stream_class
        if cls is None:
            return None
        data = self.coordinator.data
        user = quote(data.rtsp_username, safe="")
        password = quote(str(self._entry.options.get(CONF_RTSP_PASSWORD, "")), safe="")
        host = self._entry.data[CONF_HOST]
        return f"rtsp://{user}:{password}@{host}:{data.rtsp_port}/{self.camera_id}/{cls}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Latest keyframe from the recorder's RTSP port."""
        cls = self.stream_class
        if cls is None:
            return None
        data = self.coordinator.data
        return await self.coordinator.client.fetch_snapshot(
            self.camera_id, cls, data.rtsp_username,
            str(self._entry.options.get(CONF_RTSP_PASSWORD, "")), width,
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Stream facts the recorder reports."""
        st = self.coordinator.data.stream_health(self.camera_id, self.stream_class or "sub") or {}
        return {
            "stream_class": self.stream_class,
            "codec": st.get("codec"),
            "width": st.get("width"),
            "height": st.get("height"),
            "fps": st.get("fps"),
        }
