"""Reverse proxy that serves the recorder's web UI from Home Assistant's origin.

Why: the recorder's pages live behind its own HTTPS listener with a
certificate issued by the recorder's root and an admin login. Framing them
directly means trusting that root on every device and logging in inside
the frame. Served from Home Assistant's own origin instead, the browser
sees only Home Assistant's certificate, the integration injects the admin
bearer on the way to the recorder, and the Home Assistant apps work.

The pages use root-absolute paths (/api/v1/…, /hls/…, /whep/…, /local/…),
so text responses and Location headers are rewritten to the proxy prefix.
Binary bodies, playlists with relative URIs, clip downloads and the
detections event stream are streamed through untouched (the event stream
line by line, so its JSON can be rewritten without buffering).

The talk-down WebSocket is bridged message by message. Access: a browser
is let in with the panel session cookie that the sidebar panel establishes
(panel.py), or with a Home Assistant bearer. The
recorder's own log-out and password verbs are intercepted: a click on
"Log out" inside the frame ends only the panel session, never the admin
session the integration runs on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web
from aiohttp.helpers import must_be_empty_body
from multidict import CIMultiDict

from homeassistant.components.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .api import AuviAuthError, AuviConnectionError
from .const import COOKIE_NAME, DOMAIN, PROXY_PREFIX, SESSION_TTL

if TYPE_CHECKING:
    from . import AuviConfigEntry

LOGGER = logging.getLogger(__package__)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
STRIP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host", "cookie", "referer", "origin", "authorization", "accept-encoding",
    "content-length",
}
STRIP_RESPONSE_HEADERS = HOP_BY_HOP | {
    "content-encoding", "set-cookie", "x-frame-options", "content-security-policy",
    "content-security-policy-report-only", "strict-transport-security", "server",
    "referrer-policy",
}
REWRITE_CONTENT_TYPES = (
    "text/html", "text/css", "text/javascript", "application/javascript",
    "application/x-javascript", "application/json", "image/svg+xml",
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
)
EVENT_STREAM = "text/event-stream"

# One pass: a quote, backtick, "(" or "=" followed by one of the recorder's
# root paths. Already-prefixed paths start with "/api/auvi_nvr/", which the
# single pass never revisits.
_PATH_RE = re.compile(rb'(?P<q>["\'`(=])/(?P<p>api|local|hls|whep)/')

# (method, path without leading slash) → handled locally.
INTERCEPT_LOGOUT = {("POST", "api/v1/local/logout"), ("POST", "api/v1/local/logout-all")}
INTERCEPT_NOOP = {("POST", "api/v1/local/login")}
INTERCEPT_FORBIDDEN = {("PUT", "api/v1/local/admin-password")}

REAUTH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Auvi NVR</title>
<style>body{margin:0;display:flex;align-items:center;justify-content:center;height:100vh;
font:15px system-ui,sans-serif;color:#8b9bb0;background:#0b0f14}</style></head>
<body><div>Reconnecting to the recorder…</div>
<script>
try { parent.postMessage({type: "auvi-nvr:reauth"}, location.origin); } catch (e) {}
</script></body></html>
"""


def rewrite_body(data: bytes, prefix: bytes, origin: bytes) -> bytes:
    """Prefix the recorder's root paths and absolute origin URLs."""
    out = _PATH_RE.sub(lambda m: m.group("q") + prefix + b"/" + m.group("p") + b"/", data)
    if origin:
        out = out.replace(origin + b"/", prefix + b"/")
    return out


def rewrite_location(value: str, prefix: str, origin: str) -> str:
    """Prefix an absolute-path or recorder-origin Location."""
    if value.startswith("/"):
        return prefix + value
    if origin and value.startswith(origin + "/"):
        return prefix + value[len(origin):]
    return value


def request_headers(request: web.Request, prefix: str) -> dict[str, str]:
    """Headers forwarded to the recorder (no cookies, no browser origin)."""
    out: dict[str, str] = {}
    for name, value in request.headers.items():
        lname = name.lower()
        if lname in STRIP_REQUEST_HEADERS or lname.startswith("sec-fetch-"):
            continue
        out[name] = value
    out["Accept-Encoding"] = "identity"
    out["X-Ingress-Path"] = prefix
    if request.remote:
        out["X-Forwarded-For"] = request.remote
    out["X-Forwarded-Proto"] = request.url.scheme
    out["X-Forwarded-Host"] = request.host
    return out


def response_headers(
    upstream: aiohttp.ClientResponse, prefix: str, origin: str, drop_length: bool
) -> CIMultiDict[str]:
    """Headers passed back to the browser."""
    out: CIMultiDict[str] = CIMultiDict()
    for name, value in upstream.headers.items():
        lname = name.lower()
        if lname in STRIP_RESPONSE_HEADERS or lname.startswith("access-control-"):
            continue
        if lname == "content-length" and drop_length:
            continue
        if lname in ("location", "content-location"):
            value = rewrite_location(value, prefix, origin)
        out.add(name, value)
    if "Cache-Control" not in out:
        out["Cache-Control"] = "no-store"
    return out


class AuviProxyView(HomeAssistantView):
    """Everything under /api/auvi_nvr/<entry_id>/ forwards to the recorder."""

    url = PROXY_PREFIX + "/{entry_id}/{path:.*}"
    name = "api:auvi_nvr:proxy"
    requires_auth = False
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Forward GET."""
        return await self._handle(request, entry_id, path)

    async def post(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Forward POST."""
        return await self._handle(request, entry_id, path)

    async def put(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Forward PUT."""
        return await self._handle(request, entry_id, path)

    async def delete(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Forward DELETE."""
        return await self._handle(request, entry_id, path)

    async def patch(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Forward PATCH."""
        return await self._handle(request, entry_id, path)

    async def head(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Forward HEAD."""
        return await self._handle(request, entry_id, path)

    # No OPTIONS handler: Home Assistant attaches its own CORS preflight
    # handler to every view route and refuses a second one at registration.
    # The framed pages are same-origin and never preflight.

    # ---- helpers -----------------------------------------------------------

    def _entry(self, entry_id: str) -> AuviConfigEntry | None:
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return None
        return entry

    @staticmethod
    def _authorized(request: web.Request, entry: AuviConfigEntry) -> bool:
        if request.get(KEY_AUTHENTICATED):
            return True
        sid = request.cookies.get(COOKIE_NAME)
        if not sid:
            return False
        sessions = entry.runtime_data.panel_sessions
        sess = sessions.get(sid)
        if sess is None:
            return False
        now = time.monotonic()
        if now - sess.last_seen > SESSION_TTL:
            sessions.pop(sid, None)
            return False
        sess.last_seen = now
        return True

    async def _handle(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        entry = self._entry(entry_id)
        if entry is None:
            return web.json_response({"error": "unknown or unloaded recorder"}, status=404)
        prefix = f"{PROXY_PREFIX}/{entry_id}"
        if not self._authorized(request, entry):
            if "text/html" in request.headers.get("Accept", ""):
                return web.Response(
                    text=REAUTH_HTML, content_type="text/html", status=401,
                    headers={"Cache-Control": "no-store"},
                )
            return web.json_response({"error": "panel_session_required"}, status=401)

        method = request.method.upper()
        key = (method, path.strip("/"))
        if key in INTERCEPT_LOGOUT:
            sid = request.cookies.get(COOKIE_NAME)
            if sid:
                entry.runtime_data.panel_sessions.pop(sid, None)
            resp = web.Response(status=204)
            resp.del_cookie(COOKIE_NAME, path=prefix + "/")
            return resp
        if key in INTERCEPT_NOOP:
            return web.Response(status=204)
        if key in INTERCEPT_FORBIDDEN:
            return web.json_response(
                {"error": "change the admin password on the recorder itself, then update the integration"},
                status=403,
            )
        if key[1] == "enter":
            return web.json_response({"error": "not found"}, status=404)

        client = entry.runtime_data.client
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._bridge_websocket(request, client, "/" + path)

        # Bodies are streamed to the recorder: a model upload from the
        # console is far larger than Home Assistant's request buffer.
        body = None
        if method not in ("GET", "HEAD", "OPTIONS") and request.can_read_body:
            body = request.content
        try:
            upstream = await client.proxy_request(
                method, "/" + path, request.query_string, request_headers(request, prefix), body
            )
        except AuviAuthError as err:
            LOGGER.warning("Recorder refused the integration's login: %s", err)
            return web.json_response({"error": "recorder_auth_failed"}, status=502)
        except AuviConnectionError as err:
            return web.json_response({"error": str(err)}, status=502)
        try:
            return await self._relay(request, upstream, prefix, client.origin)
        except asyncio.CancelledError:
            upstream.close()
            raise
        except (ConnectionResetError, aiohttp.ClientError) as err:
            LOGGER.debug("proxy relay ended early for %s: %s", path, err)
            upstream.close()
            raise web.HTTPBadGateway() from err
        finally:
            upstream.release()

    async def _bridge_websocket(self, request: web.Request, client, path: str) -> web.StreamResponse:
        """Pump a browser WebSocket (talk-down audio) to the recorder and back."""
        try:
            upstream = await client.ws_connect(path, request.query_string)
        except (AuviAuthError, AuviConnectionError) as err:
            return web.json_response({"error": str(err)}, status=502)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        async def pump_down() -> None:
            async for msg in upstream:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        async def pump_up() -> None:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await upstream.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await upstream.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        tasks = [asyncio.create_task(pump_down()), asyncio.create_task(pump_up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await upstream.close()
            if not ws.closed:
                await ws.close()
        return ws

    async def _relay(
        self, request: web.Request, upstream: aiohttp.ClientResponse, prefix: str, origin: str
    ) -> web.StreamResponse:
        ctype = upstream.headers.get("Content-Type", "").lower()
        rewriting = ctype.startswith(REWRITE_CONTENT_TYPES)
        headers = response_headers(upstream, prefix, origin, drop_length=rewriting)
        if must_be_empty_body(request.method, upstream.status):
            headers.popall("Content-Length", None)
            return web.Response(status=upstream.status, headers=headers)
        bprefix, borigin = prefix.encode(), origin.encode()
        if rewriting:
            raw = await upstream.read()
            return web.Response(status=upstream.status, body=rewrite_body(raw, bprefix, borigin), headers=headers)

        stream = web.StreamResponse(status=upstream.status, headers=headers)
        await stream.prepare(request)
        if ctype.startswith(EVENT_STREAM):
            pending = b""
            async for chunk in upstream.content.iter_any():
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                if lines:
                    await stream.write(rewrite_body(b"\n".join(lines) + b"\n", bprefix, borigin))
            if pending:
                await stream.write(rewrite_body(pending, bprefix, borigin))
        else:
            async for chunk in upstream.content.iter_any():
                await stream.write(chunk)
        await stream.write_eof()
        return stream
