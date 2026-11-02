"""Polling coordinator for one recorder."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuviAuthError, AuviClient, AuviConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FRAME_STALL_MS,
    INFO_REFRESH_EVERY,
)

LOGGER = logging.getLogger(__package__)


@dataclass
class AuviData:
    """One poll of the recorder."""

    health: dict[str, Any]
    cameras: dict[str, dict[str, Any]] = field(default_factory=dict)
    infos: dict[str, dict[str, Any]] = field(default_factory=dict)
    rtsp: dict[str, Any] = field(default_factory=dict)
    tls: dict[str, Any] = field(default_factory=dict)

    # ---- derived views -----------------------------------------------------

    @property
    def agent(self) -> dict[str, Any]:
        """The health document's agent block."""
        return self.health.get("agent") or {}

    @property
    def host_block(self) -> dict[str, Any]:
        """The health document's host block."""
        return self.agent.get("host") or {}

    def camera_health(self, camera_id: str) -> dict[str, Any] | None:
        """The per-camera health block, if the camera is reported."""
        for cam in self.health.get("cameras") or []:
            if cam.get("camera_id") == camera_id:
                return cam
        return None

    def stream_health(self, camera_id: str, stream_class: str) -> dict[str, Any] | None:
        """One stream's health block."""
        cam = self.camera_health(camera_id)
        if not cam:
            return None
        for st in cam.get("streams") or []:
            if st.get("class") == stream_class:
                return st
        return None

    def camera_online(self, camera_id: str) -> bool | None:
        """True when any stream is connected and delivering frames."""
        cam = self.camera_health(camera_id)
        if cam is None:
            return None
        streams = cam.get("streams") or []
        if not streams:
            return False
        return any(
            st.get("conn_state") == "connected"
            and int(st.get("last_frame_age_ms") or FRAME_STALL_MS + 1) <= FRAME_STALL_MS
            for st in streams
        )

    def camera_recording(self, camera_id: str) -> bool | None:
        """True when any stream is writing segments."""
        cam = self.camera_health(camera_id)
        if cam is None:
            return None
        return any(
            st.get("recording_state") == "recording" for st in cam.get("streams") or []
        )

    def rtsp_camera(self, camera_id: str) -> dict[str, Any] | None:
        """The RTSP passthrough sharing block for a camera."""
        for cam in self.rtsp.get("cameras") or []:
            if cam.get("id") == camera_id:
                return cam
        return None

    def shared_classes(self, camera_id: str) -> list[str]:
        """Stream classes the recorder re-serves over RTSP for a camera."""
        if not (self.rtsp.get("enabled") and self.rtsp.get("credential")):
            return []
        cam = self.rtsp_camera(camera_id)
        if not cam:
            return []
        return [cls for cls in ("sub", "main") if cam.get(cls)]

    @property
    def rtsp_username(self) -> str:
        """The RTSP passthrough login name."""
        return str(self.rtsp.get("username") or "")

    @property
    def rtsp_port(self) -> int:
        """The RTSP passthrough port."""
        return int(self.rtsp.get("port") or 8554)


class AuviCoordinator(DataUpdateCoordinator[AuviData]):
    """Polls health, cameras and RTSP sharing; device facts every Nth poll."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AuviClient) -> None:
        """Initialise."""
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=interval),
            always_update=False,
        )
        self.client = client
        self._tick = 0
        self._infos: dict[str, dict[str, Any]] = {}
        self._tls: dict[str, Any] = {}

    async def _async_update_data(self) -> AuviData:
        try:
            health, cameras, rtsp = await asyncio.gather(
                self.client.get_health(),
                self.client.get_cameras(),
                self.client.get_rtsp_server(),
            )
            if self._tick % INFO_REFRESH_EVERY == 0 or not self._infos:
                self._infos = await self.client.get_cameras_info()
                self._tls = await self.client.get_tls()
            self._tick += 1
        except AuviAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AuviConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return AuviData(
            health=health,
            cameras={c["id"]: c for c in cameras if c.get("id")},
            infos=self._infos,
            rtsp=rtsp,
            tls=self._tls,
        )
