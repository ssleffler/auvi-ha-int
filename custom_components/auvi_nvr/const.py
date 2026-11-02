"""Constants for the Auvi NVR integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "auvi_nvr"
VERSION = "0.1.0"
MANUFACTURER = "Auvi"
RECORDER_MODEL = "Auvi Video Recorder"

PLATFORMS: list[Platform] = [Platform.CAMERA, Platform.BINARY_SENSOR, Platform.SENSOR]

# Config entry data
CONF_RTSP_PASSWORD = "rtsp_password"
CONF_STREAM_CLASS = "stream_class"
CONF_CA_PEM = "ca_pem"
CONF_CA_FINGERPRINT = "ca_fingerprint"
CONF_PANEL_REQUIRE_ADMIN = "panel_require_admin"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_USERNAME = "admin"
DEFAULT_PORT = 8443
DEFAULT_RTSP_PORT = 8554
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300
DEFAULT_STREAM_CLASS = "sub"
STREAM_CLASSES = ["sub", "main"]
# Camera device details (vendor, model, firmware) change rarely: refresh
# them every Nth poll.
INFO_REFRESH_EVERY = 10

# Sidebar panel + proxy. The proxy must live under /api/: the Home
# Assistant service worker caches every other same-origin path
# stale-while-revalidate, which would break event streams and segments.
PROXY_PREFIX = "/api/auvi_nvr"
STATIC_URL = f"/auvi_nvr_static/{VERSION}"
PANEL_URL_PATH = "auvi-nvr"
PANEL_ELEMENT = "auvi-nvr-panel"
PANEL_TITLE = "Auvi NVR"
PANEL_ICON = "mdi:cctv"
COOKIE_NAME = "auvi_nvr_session"
SESSION_TTL = 12 * 60 * 60
ENTER_SIGNATURE_SECONDS = 60
WS_TYPE_LIST = "auvi_nvr/list"
WS_TYPE_PANEL_SESSION = "auvi_nvr/panel_session"

DATA_HTTP_REGISTERED = f"{DOMAIN}_http_registered"
DATA_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"

# Health document values the recorder uses.
RECORDER_STATUSES = ["ok", "degraded", "down", "unknown"]
FRAME_STALL_MS = 10_000
