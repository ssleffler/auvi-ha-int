"""Real-time events: the recorder's rules engine posts to a webhook here.

The recorder (ADR-0543) can send one HTTP request from its own rules
engine every time an event row is written: a detector class (person,
vehicle, animal, face, license_plate), motion, an analytic (line_cross,
loiter, intrusion) or a camera-integrity event (view_change, tamper). This
module keeps one such rule per camera on the recorder, aimed at a webhook
this integration registers, authenticated with a bearer secret the
integration stores in the recorder's keystore. Every post becomes an
``auvi_nvr_event`` on the event bus, a dispatcher signal for the detection
sensors, and the data the device triggers match on.

The recorder has no "always" window (``sun`` is night only, ``custom`` is a
from/to clock), so each camera gets a 00:00-23:59 rule and one shared
rule covers the last minute of the day.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
import secrets
import time
from typing import Any

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError

from .api import AuviAuthError, AuviClient, AuviConnectionError
from .const import (
    ANALYTIC_EVENTS,
    CONF_EVENT_SECRET,
    CONF_EVENTS_BASE_URL,
    CONF_EVENTS_ENABLED,
    CONF_EVENTS_INSECURE_TLS,
    CONF_EVENTS_LOCAL_ONLY,
    DOMAIN,
    EVENT_KINDS,
    EVENT_TYPE,
    OBJECT_CLASSES,
    RULE_COOLDOWN_S,
    RULE_NAME_PREFIX,
    SIGNAL_EVENT,
    WEBHOOK_TIMEOUT_S,
)
from .coordinator import AuviData
from .entity import camera_identifier

LOGGER = logging.getLogger(__package__)

# A rejected post means the recorder holds a different secret (restored
# backup, re-installed recorder): push ours again, at most this often.
SECRET_REPUSH_INTERVAL = 600
# Metadata keys worth copying onto entity attributes (the full document,
# with tracks and paths, goes on the event bus only).
ATTRIBUTE_KEYS = (
    "label", "subclass", "confidence", "zones", "plate", "plate_region",
    "plate_conf", "plate_status", "identity", "identity_label", "identity_list",
    "people", "upper_color", "lower_color",
)
BULKY_METADATA_KEYS = ("track", "paths")
# {{var}} placeholders are rendered by the recorder per fire; metadata is
# inserted raw because it is JSON itself.
BODY_TEMPLATE = (
    '{"camera_id":"{{camera_id}}","camera_name":"{{camera_name}}",'
    '"event_type":"{{event_type}}","event_id":"{{event_id}}",'
    '"time":"{{time}}","time_unix_ms":"{{time_unix_ms}}",'
    '"rule_id":"{{rule_id}}","rule_name":"{{rule_name}}",'
    '"recorder_id":"{{recorder_id}}","metadata":{{metadata}}}'
)


@dataclass
class AuviEvent:
    """One event the recorder posted."""

    camera_id: str
    camera_name: str
    event_type: str
    event_id: str
    time_ms: int
    rule_id: str
    rule_name: str
    metadata: dict[str, Any]
    received: float = field(default_factory=time.monotonic)

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> AuviEvent:
        """Parse a posted body; raises ValueError when it is not an event."""
        camera_id = str(body.get("camera_id") or "")
        event_type = str(body.get("event_type") or "")
        if not camera_id or not event_type:
            raise ValueError("camera_id and event_type are required")
        try:
            time_ms = int(body.get("time_unix_ms") or 0)
        except (TypeError, ValueError):
            time_ms = 0
        if time_ms <= 0:
            time_ms = int(time.time() * 1000)
        metadata = body.get("metadata")
        return cls(
            camera_id=camera_id,
            camera_name=str(body.get("camera_name") or camera_id),
            event_type=event_type,
            event_id=str(body.get("event_id") or ""),
            time_ms=time_ms,
            rule_id=str(body.get("rule_id") or ""),
            rule_name=str(body.get("rule_name") or ""),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    @property
    def when(self) -> datetime:
        """The event time (UTC)."""
        return datetime.fromtimestamp(self.time_ms / 1000, tz=timezone.utc)

    @property
    def is_activity(self) -> bool:
        """An object or analytic event (not motion, not integrity)."""
        return self.event_type in OBJECT_CLASSES or self.event_type in ANALYTIC_EVENTS

    @property
    def plate(self) -> str:
        """The consensus plate reading, if any."""
        return str(self.metadata.get("plate") or "")

    def attributes(self) -> dict[str, Any]:
        """Compact attributes for an entity."""
        out: dict[str, Any] = {
            "event_type": self.event_type,
            "event_id": self.event_id,
            "event_time": self.when.isoformat(),
            "camera_id": self.camera_id,
        }
        for key in ATTRIBUTE_KEYS:
            if (value := self.metadata.get(key)) not in (None, "", [], {}):
                out[key] = value
        return out

    def bus_data(self, entry_id: str, recorder: str, device_id: str | None) -> dict[str, Any]:
        """The event-bus payload (without the per-frame track and path samples,
        which can push an event past what the recorder component stores)."""
        metadata = {k: v for k, v in self.metadata.items() if k not in BULKY_METADATA_KEYS}
        return {
            "entry_id": entry_id,
            "recorder": recorder,
            "device_id": device_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "type": self.event_type,
            "activity": self.is_activity,
            "event_id": self.event_id,
            "time": self.when.isoformat(),
            "time_unix_ms": self.time_ms,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "plate": self.plate,
            "metadata": metadata,
        }


class AuviEventBridge:
    """Owns the webhook for one recorder and the rules that feed it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AuviClient, uid: str) -> None:
        """Initialise (call async_start to register the webhook)."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.uid = uid
        self.webhook_id: str = entry.data[CONF_WEBHOOK_ID]
        self.secret: str = entry.data[CONF_EVENT_SECRET]
        self.last: dict[str, AuviEvent] = {}
        self.received = 0
        self.rejected = 0
        self.rules_error: str | None = None
        self.rules_synced: dict[str, str] = {}
        self._synced_key: tuple[Any, ...] | None = None
        self._lock = asyncio.Lock()
        self._secret_pushed_at = 0.0
        self._registered = False
        self._unsupported_logged = False

    # ---- lifecycle -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether the event feed is switched on in the options."""
        return bool(self.entry.options.get(CONF_EVENTS_ENABLED, True))

    @property
    def secret_name(self) -> str:
        """The recorder keystore name of our bearer (letters, digits, . _ -)."""
        return f"home-assistant-{self.webhook_id[:16]}"

    @property
    def webhook_path(self) -> str:
        """The webhook's path on Home Assistant."""
        return webhook.async_generate_path(self.webhook_id)

    @callback
    def async_start(self) -> None:
        """Register the webhook."""
        if self._registered:
            return
        webhook.async_register(
            self.hass,
            DOMAIN,
            f"Auvi NVR {self.entry.title}",
            self.webhook_id,
            self._handle_webhook,
            local_only=bool(self.entry.options.get(CONF_EVENTS_LOCAL_ONLY, True)),
            allowed_methods=["POST"],
        )
        self._registered = True

    @callback
    def async_stop(self) -> None:
        """Unregister the webhook (the recorder's rules stay for the next load)."""
        if self._registered:
            webhook.async_unregister(self.hass, self.webhook_id)
            self._registered = False

    # ---- receiving ----------------------------------------------------------

    async def _handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        # compare_digest wants ASCII str or bytes; a stray non-ASCII header must
        # not turn into a TypeError (which the webhook view answers with 200).
        presented = token.strip().encode("utf-8", "surrogateescape")
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented, self.secret.encode()):
            self.rejected += 1
            if self.rejected in (1, 10, 100) or self.rejected % 1000 == 0:
                LOGGER.warning(
                    "Event post for %s refused: the recorder's secret does not match "
                    "(pushing ours again)",
                    self.entry.title,
                )
            self._maybe_repush_secret()
            return web.Response(status=401, text="event secret mismatch")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("not an object")
            event = AuviEvent.from_body(body)
        except ValueError as err:
            LOGGER.debug("Ignoring a post that is not an event: %s", err)
            return web.Response(status=400, text=f"not an event: {err}")
        self._record(event)
        return web.Response(status=204)

    @callback
    def _record(self, event: AuviEvent) -> None:
        self.received += 1
        self.last[event.camera_id] = event
        device = dr.async_get(self.hass).async_get_device(
            identifiers={camera_identifier(self.uid, event.camera_id)}
        )
        self.hass.bus.async_fire(
            EVENT_TYPE,
            event.bus_data(self.entry.entry_id, self.uid, device.id if device else None),
        )
        async_dispatcher_send(self.hass, SIGNAL_EVENT.format(self.entry.entry_id), event)

    @callback
    def _maybe_repush_secret(self) -> None:
        if not self.enabled:
            # The feed is off and our secret was removed from the recorder;
            # a stray post must not put it back.
            return
        now = time.monotonic()
        if now - self._secret_pushed_at < SECRET_REPUSH_INTERVAL:
            return
        self._secret_pushed_at = now
        self.entry.async_create_background_task(
            self.hass, self._push_secret(), f"{DOMAIN} secret push"
        )

    async def _push_secret(self) -> None:
        try:
            await self.client.set_secret(self.secret_name, self.secret)
            self._secret_pushed_at = time.monotonic()
        except (AuviAuthError, AuviConnectionError) as err:
            LOGGER.warning("Could not store the event secret on %s: %s", self.entry.title, err)

    # ---- rules on the recorder ------------------------------------------------

    def _webhook_url(self) -> str | None:
        base = str(self.entry.options.get(CONF_EVENTS_BASE_URL) or "").strip().rstrip("/")
        if base:
            return base + self.webhook_path
        try:
            return webhook.async_generate_url(
                self.hass,
                self.webhook_id,
                allow_internal=True,
                allow_external=True,
                prefer_external=False,
            )
        except NoURLAvailableError:
            return None

    def _rule(
        self,
        name: str,
        subject_kind: str,
        subject_id: str,
        triggers: list[str],
        url: str,
        start: str,
        end: str,
        insecure: bool,
    ) -> dict[str, Any]:
        return {
            "id": "",
            "name": name[:80],
            "enabled": True,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "triggers": triggers,
            "window": "custom",
            "from": start,
            "to": end,
            "action_kind": "webhook",
            "action_duration_s": 0,
            "webhook": {
                "method": "POST",
                "url": url,
                "body": BODY_TEMPLATE,
                "auth": "bearer",
                "secret_name": self.secret_name,
                "insecure_tls": insecure,
                "timeout_s": WEBHOOK_TIMEOUT_S,
            },
            "cooldown_s": RULE_COOLDOWN_S,
        }

    @staticmethod
    def _same(existing: dict[str, Any], wanted: dict[str, Any]) -> bool:
        """Whether a stored rule already equals what we would write."""
        for key in ("name", "subject_kind", "subject_id", "window", "from", "to", "action_kind"):
            if str(existing.get(key) or "") != wanted[key]:
                return False
        if bool(existing.get("enabled")) != wanted["enabled"]:
            return False
        if int(existing.get("cooldown_s") or 0) != wanted["cooldown_s"]:
            return False
        if sorted(existing.get("triggers") or []) != sorted(wanted["triggers"]):
            return False
        have = existing.get("webhook") or {}
        want = wanted["webhook"]
        for key in ("method", "url", "body", "auth", "secret_name"):
            if str(have.get(key) or "") != want[key]:
                return False
        if bool(have.get("insecure_tls")) != want["insecure_tls"]:
            return False
        return int(have.get("timeout_s") or 0) == want["timeout_s"]

    def _ours(self, doc: dict[str, Any]) -> list[dict[str, Any]]:
        """The recorder-local rules that post to our webhook."""
        path = self.webhook_path
        return [
            r
            for r in doc.get("rules") or []
            if r.get("editable", r.get("origin") == "local")
            and r.get("action_kind") == "webhook"
            and str((r.get("webhook") or {}).get("url") or "").endswith(path)
        ]

    async def async_sync_rules(self, data: AuviData) -> None:
        """Make the recorder's rules match the cameras it has (idempotent)."""
        cameras = {
            cam_id: cam for cam_id, cam in data.cameras.items() if cam.get("mode") != "speaker"
        }
        url = self._webhook_url() if self.enabled else None
        insecure = bool(self.entry.options.get(CONF_EVENTS_INSECURE_TLS, False))
        key: tuple[Any, ...] = (
            self.enabled,
            url,
            insecure,
            tuple(sorted((cam_id, str(cam.get("name") or "")) for cam_id, cam in cameras.items())),
        )
        if key == self._synced_key:
            return
        if self.enabled and url is None:
            if self.rules_error != "no_url":
                LOGGER.warning(
                    "Events from %s need a URL the recorder can post to: set Home "
                    "Assistant's internal URL, or the event URL in the integration options",
                    self.entry.title,
                )
            self.rules_error = "no_url"
            return
        async with self._lock:
            if key == self._synced_key:
                return
            try:
                partial = await self._sync_locked(cameras, url, insecure)
            except AuviConnectionError as err:
                text = str(err)
                # Only the rules document itself missing means "too old"; a 404
                # on one rule's PUT just means it was deleted under us.
                if text.startswith("GET /api/v1/local/rules:") and "HTTP 404" in text:
                    if not self._unsupported_logged:
                        LOGGER.warning(
                            "%s has no rules API (recorder too old): no real-time events",
                            self.entry.title,
                        )
                        self._unsupported_logged = True
                    self.rules_error = "unsupported"
                    self._synced_key = key
                    return
                LOGGER.warning("Could not install event rules on %s: %s", self.entry.title, err)
                self.rules_error = text
                return
            except AuviAuthError as err:
                LOGGER.warning("Could not install event rules on %s: %s", self.entry.title, err)
                self.rules_error = str(err)
                return
            if partial:
                # Some cameras were refused (e.g. not running on the recorder):
                # keep the others and try the refused ones again next poll.
                self.rules_error = partial
                return
            self.rules_error = None
            self._synced_key = key

    @staticmethod
    def _refused(err: AuviConnectionError) -> bool:
        """Whether the recorder answered a request with a 4xx (not a transport failure)."""
        match = re.search(r": HTTP (\d{3})\b", str(err))
        return bool(match) and match.group(1).startswith("4")

    async def _sync_locked(
        self, cameras: dict[str, dict[str, Any]], url: str | None, insecure: bool
    ) -> str | None:
        """Write the rules; returns a summary of per-rule refusals, or None."""
        doc = await self.client.get_rules()
        ours = self._ours(doc)
        secret_names = {s.get("name") for s in doc.get("secrets") or []}
        if url is None:
            for rule in ours:
                await self.client.delete_rule(str(rule.get("id")))
            if self.secret_name in secret_names:
                await self.client.delete_secret(self.secret_name)
            self.rules_synced = {}
            if ours:
                LOGGER.info("Removed %d event rules from %s", len(ours), self.entry.title)
            return None

        if self.secret_name not in secret_names:
            await self.client.set_secret(self.secret_name, self.secret)
            self._secret_pushed_at = time.monotonic()

        vocabulary = [t for t in doc.get("triggers") or [] if t != "any"] or list(EVENT_KINDS)
        wanted: dict[str, dict[str, Any]] = {}
        for cam_id, cam in cameras.items():
            name = str(cam.get("name") or cam_id)
            wanted[cam_id] = self._rule(
                f"{RULE_NAME_PREFIX}: {name}", "camera", cam_id, vocabulary, url,
                "00:00", "23:59", insecure,
            )
        if cameras:
            wanted["*"] = self._rule(
                f"{RULE_NAME_PREFIX}: all cameras (23:59)", "all", "", vocabulary, url,
                "23:59", "00:00", insecure,
            )

        by_subject: dict[str, list[dict[str, Any]]] = {}
        for rule in ours:
            subject = "*" if rule.get("subject_kind") == "all" else str(rule.get("subject_id") or "")
            by_subject.setdefault(subject, []).append(rule)

        created = updated = deleted = 0
        synced: dict[str, str] = {}
        refused: list[str] = []
        for subject, rule in wanted.items():
            existing = by_subject.pop(subject, [])
            keep = existing[0] if existing else None
            for extra in existing[1:]:
                await self.client.delete_rule(str(extra.get("id")))
                deleted += 1
            try:
                if keep is None:
                    stored = await self.client.create_rule(rule)
                    created += 1
                elif self._same(keep, rule):
                    stored = keep
                else:
                    stored = await self.client.update_rule(str(keep.get("id")), rule)
                    updated += 1
            except AuviConnectionError as err:
                if not self._refused(err):
                    raise
                # A 400 "unknown camera" for one that is not running on the
                # recorder, or a 404 for a rule deleted under us: skip this
                # one, keep going, and try it again on the next poll.
                refused.append(f"{rule['name']}: {err}")
                continue
            synced[subject] = str(stored.get("id") or "")
        for leftovers in by_subject.values():
            for rule in leftovers:
                await self.client.delete_rule(str(rule.get("id")))
                deleted += 1
        self.rules_synced = synced
        if created or updated or deleted:
            LOGGER.info(
                "Event rules on %s: %d created, %d updated, %d removed (%d cameras)",
                self.entry.title, created, updated, deleted, len(cameras),
            )
        if not refused:
            return None
        summary = "; ".join(refused)
        if summary != self.rules_error:
            LOGGER.warning(
                "%s refused %d of %d event rules (will retry): %s",
                self.entry.title, len(refused), len(wanted), summary,
            )
        return summary

    async def async_remove_from_recorder(self) -> None:
        """Delete our rules and secret (the entry is being removed)."""
        try:
            doc = await self.client.get_rules()
            for rule in self._ours(doc):
                await self.client.delete_rule(str(rule.get("id")))
            if self.secret_name in {s.get("name") for s in doc.get("secrets") or []}:
                await self.client.delete_secret(self.secret_name)
        except (AuviAuthError, AuviConnectionError) as err:
            LOGGER.warning(
                "Could not remove the event rules from %s (remove them on its Rules tab): %s",
                self.entry.title, err,
            )
