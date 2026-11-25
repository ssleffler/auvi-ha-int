"""Constants for the Auvi NVR integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "auvi_nvr"
VERSION = "0.2.1"
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
# The bearer the recorder presents on every event post (entry data,
# generated once; stored on the recorder under the rule secret name).
CONF_EVENT_SECRET = "event_secret"

# Options for the real-time event feed
CONF_EVENTS_ENABLED = "events_enabled"
CONF_EVENTS_BASE_URL = "events_base_url"
CONF_EVENTS_LOCAL_ONLY = "events_local_only"
CONF_EVENTS_INSECURE_TLS = "events_insecure_tls"
CONF_EVENT_HOLD = "event_hold"

DEFAULT_USERNAME = "admin"
DEFAULT_PORT = 8443
DEFAULT_RTSP_PORT = 8554
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300
DEFAULT_STREAM_CLASS = "sub"
STREAM_CLASSES = ["sub", "main"]
DEFAULT_EVENT_HOLD = 30
MIN_EVENT_HOLD = 5
MAX_EVENT_HOLD = 600
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

# Real-time events (the recorder's rules engine, ADR-0543, posting to a
# webhook this integration owns). Event types are the recorder's trigger
# vocabulary: detector classes, motion, analytics and camera integrity.
EVENT_TYPE = f"{DOMAIN}_event"
SIGNAL_EVENT = f"{DOMAIN}_event_{{}}"  # .format(entry_id)
OBJECT_CLASSES = ["person", "vehicle", "animal", "face", "license_plate"]
ANALYTIC_EVENTS = ["line_cross", "loiter", "intrusion"]
INTEGRITY_EVENTS = ["view_change", "tamper"]
EVENT_KINDS = ["motion", *OBJECT_CLASSES, *ANALYTIC_EVENTS, *INTEGRITY_EVENTS]
# "activity" is any object or analytic event (not motion, not integrity).
TRIGGER_ACTIVITY = "activity"
RULE_NAME_PREFIX = "Home Assistant"
RULE_COOLDOWN_S = 1
WEBHOOK_TIMEOUT_S = 5
