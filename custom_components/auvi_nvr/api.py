"""HTTP client for an Auvi NVR recorder's local media server.

The recorder serves its local web UI and media APIs over HTTPS (port 8443
by default) with a certificate issued by its own root, which it offers at
``GET /local/recorder.crt`` without authentication. The client pins that
root as its only trust anchor, logs in once with the ``admin`` password
(``POST /api/v1/local/login``) and sends the resulting bearer on every
request; the session slides for 30 days on the recorder, and the client
re-logs in on a 401 or when the expiry comes near.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import time
from typing import Any

import aiohttp
from aiohttp import ClientResponse, ClientTimeout
from yarl import URL

from .const import DEFAULT_RTSP_PORT

LOGGER = logging.getLogger(__package__)

TOKEN_RENEW_BEFORE_MS = 24 * 60 * 60 * 1000
API_TIMEOUT = ClientTimeout(total=20)
LOGIN_TIMEOUT = ClientTimeout(total=15)
CERT_TIMEOUT = ClientTimeout(total=10)
SNAPSHOT_TIMEOUT = ClientTimeout(total=10)


class AuviError(Exception):
    """Base error."""


class AuviConnectionError(AuviError):
    """The recorder could not be reached or answered unexpectedly."""


class AuviCertError(AuviConnectionError):
    """The recorder's certificate is not the one we trust."""


class AuviAuthError(AuviError):
    """The admin credentials were refused."""


class AuviNoPasswordError(AuviAuthError):
    """The recorder has no admin password yet (create it on the recorder)."""


def build_ssl_context(pem: str) -> ssl.SSLContext:
    """Build a client context trusting only the recorder's root.

    Blocking (reads the PEM into OpenSSL): call through an executor. The
    host name is not checked because the recorder's certificate names its
    LAN address and host name at issue time; trusting exactly one private
    root is the stronger pin.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=pem)
    return ctx


def first_pem_block(pem: str) -> str:
    """Return the first CERTIFICATE block of a PEM bundle."""
    start = pem.find("-----BEGIN CERTIFICATE-----")
    end = pem.find("-----END CERTIFICATE-----", start)
    if start < 0 or end < 0:
        raise ValueError("no certificate in PEM")
    return pem[start : end + len("-----END CERTIFICATE-----")] + "\n"


def cert_fingerprint(pem: str) -> str:
    """SHA-256 fingerprint of the first certificate, colon separated."""
    der = ssl.PEM_cert_to_DER_cert(first_pem_block(pem))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


async def fetch_recorder_cert(
    session: aiohttp.ClientSession, host: str, port: int
) -> tuple[str, str]:
    """Fetch the recorder's trust anchor (PEM) and its fingerprint.

    This is the one request made without verification: the user confirms
    the fingerprint in the config flow (trust on first use), and every
    later request is verified against it.
    """
    url = URL.build(scheme="https", host=host, port=port, path="/local/recorder.crt")
    try:
        async with session.get(url, ssl=False, timeout=CERT_TIMEOUT) as resp:
            if resp.status != 200:
                raise AuviConnectionError(
                    f"recorder answered HTTP {resp.status} for its certificate"
                )
            pem = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        raise AuviConnectionError(f"cannot reach {host}:{port}: {err}") from err
    try:
        block = first_pem_block(pem)
        return block, cert_fingerprint(block)
    except (ValueError, ssl.SSLError) as err:
        raise AuviConnectionError("the recorder did not return a certificate") from err


class AuviClient:
    """Authenticated client for one recorder."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        ssl_context: ssl.SSLContext,
        rtsp_port: int = DEFAULT_RTSP_PORT,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._ssl = ssl_context
        self.rtsp_port = rtsp_port
        self._token: str | None = None
        self._expires_ms = 0
        self._login_lock = asyncio.Lock()

    @property
    def origin(self) -> str:
        """The recorder's HTTPS origin."""
        return f"https://{self.host}:{self.port}"

    @property
    def token(self) -> str | None:
        """The current bearer, if logged in."""
        return self._token

    def url(self, path: str, query_string: str = "") -> URL:
        """Build an absolute URL for a recorder path (leading slash)."""
        if not path.startswith("/"):
            path = "/" + path
        if query_string:
            return URL.build(
                scheme="https", host=self.host, port=self.port, path=path,
                query_string=query_string,
            )
        return URL.build(scheme="https", host=self.host, port=self.port, path=path)

    # ---- login -----------------------------------------------------------

    async def login(self) -> None:
        """Log in with the admin password; replaces the bearer."""
        async with self._login_lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        body = {"username": self.username, "password": self._password}
        try:
            async with self._session.post(
                self.url("/api/v1/local/login"), json=body, ssl=self._ssl,
                timeout=LOGIN_TIMEOUT, allow_redirects=False,
            ) as resp:
                if resp.status == 401:
                    raise AuviAuthError("wrong admin password")
                if resp.status == 409:
                    raise AuviNoPasswordError("the recorder has no admin password yet")
                if resp.status == 429:
                    raise AuviConnectionError(
                        "too many failed logins; the recorder asks to wait"
                    )
                if resp.status == 403:
                    text = (await resp.text()).strip()
                    raise AuviConnectionError(f"login refused: {text or 'forbidden'}")
                if resp.status != 200:
                    raise AuviConnectionError(f"login failed: HTTP {resp.status}")
                doc = await resp.json(content_type=None)
        except aiohttp.ClientConnectorCertificateError as err:
            raise AuviCertError(f"certificate not trusted: {err}") from err
        except aiohttp.ClientSSLError as err:
            raise AuviCertError(f"TLS failure: {err}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            raise AuviConnectionError(f"cannot reach {self.origin}: {err}") from err
        token = doc.get("token") if isinstance(doc, dict) else None
        if not token:
            raise AuviConnectionError("login answered without a token")
        self._token = token
        self._expires_ms = int(doc.get("expires_at_ms") or 0)
        LOGGER.debug("Logged in to %s", self.origin)

    async def ensure_token(self) -> str:
        """Return a bearer, logging in when missing or near expiry."""
        now_ms = int(time.time() * 1000)
        if self._token and (
            self._expires_ms == 0 or self._expires_ms - now_ms > TOKEN_RENEW_BEFORE_MS
        ):
            return self._token
        await self.login()
        assert self._token is not None
        return self._token

    # ---- requests --------------------------------------------------------

    async def send(
        self,
        method: str,
        path: str,
        *,
        query_string: str = "",
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        timeout: ClientTimeout = API_TIMEOUT,
        auto_decompress: bool = True,
    ) -> ClientResponse:
        """Send one authenticated request; the caller releases the response.

        A 401 is retried once after a fresh login; a second 401 means the
        credentials are gone (reauth).
        """
        token = await self.ensure_token()
        # A streamed body (an upload through the panel) can be sent once.
        retryable = data is None or isinstance(data, (bytes, bytearray))
        for attempt in (1, 2):
            hdrs = dict(headers or {})
            hdrs["Authorization"] = f"Bearer {token}"
            try:
                resp = await self._session.request(
                    method, self.url(path, query_string), headers=hdrs, data=data,
                    json=json, ssl=self._ssl, timeout=timeout, allow_redirects=False,
                    auto_decompress=auto_decompress,
                )
            except aiohttp.ClientConnectorCertificateError as err:
                raise AuviCertError(f"certificate not trusted: {err}") from err
            except aiohttp.ClientSSLError as err:
                raise AuviCertError(f"TLS failure: {err}") from err
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                raise AuviConnectionError(f"{method} {path}: {err}") from err
            if resp.status != 401 or attempt == 2 or not retryable:
                return resp
            resp.release()
            LOGGER.debug("Bearer refused by %s; logging in again", self.origin)
            await self.login()
            token = self._token or ""
        raise AuviAuthError("the recorder refused the admin session")  # unreachable

    async def api_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send and decode a JSON API response."""
        resp = await self.send(method, path, **kwargs)
        try:
            if resp.status == 401:
                raise AuviAuthError("the recorder refused the admin session")
            if resp.status >= 400:
                text = (await resp.text()).strip()
                raise AuviConnectionError(
                    f"{method} {path}: HTTP {resp.status} {text[:200]}"
                )
            return await resp.json(content_type=None)
        finally:
            resp.release()

    # ---- typed getters -----------------------------------------------------

    async def get_health(self) -> dict[str, Any]:
        """The recorder's health document."""
        return await self.api_json("GET", "/api/v1/health")

    async def get_cameras(self) -> list[dict[str, Any]]:
        """Configured cameras with their live stream facts."""
        doc = await self.api_json("GET", "/api/v1/cameras")
        return list(doc.get("cameras") or [])

    async def get_cameras_info(self) -> dict[str, dict[str, Any]]:
        """Device facts (make, model, firmware, peripherals) keyed by camera id."""
        doc = await self.api_json("GET", "/api/v1/cameras/info")
        return {c["id"]: c for c in (doc.get("cameras") or []) if c.get("id")}

    async def get_rtsp_server(self) -> dict[str, Any]:
        """RTSP passthrough status: which cameras are shared, the login name."""
        return await self.api_json("GET", "/api/v1/local/rtsp-server")

    async def get_tls(self) -> dict[str, Any]:
        """HTTPS listener status: host name, certificate, URL hint."""
        return await self.api_json("GET", "/api/v1/local/tls")

    async def get_auth_state(self) -> dict[str, Any]:
        """Login surface state (password_set, authenticated)."""
        return await self.api_json("GET", "/api/v1/local/auth")

    # ---- RTSP-port stills --------------------------------------------------

    async def fetch_snapshot(
        self, camera_id: str, stream_class: str, rtsp_user: str, rtsp_password: str,
        width: int | None = None,
    ) -> bytes | None:
        """Latest keyframe of a shared stream as JPEG from the RTSP port.

        Plain HTTP with Basic auth, as the recorder documents for Home
        Assistant's still_image_url. None when the stream is not shared or
        has no keyframe yet, so the caller keeps its last frame.
        """
        url = URL.build(
            scheme="http", host=self.host, port=self.rtsp_port,
            path=f"/{camera_id}/{stream_class}.jpg",
        )
        if width:
            url = url.with_query({"w": str(max(64, min(1920, int(width))))})
        try:
            async with self._session.get(
                url, auth=aiohttp.BasicAuth(rtsp_user, rtsp_password),
                timeout=SNAPSHOT_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    LOGGER.debug("snapshot %s/%s: HTTP %s", camera_id, stream_class, resp.status)
                    return None
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            LOGGER.debug("snapshot %s/%s failed: %s", camera_id, stream_class, err)
            return None

    # ---- proxy -------------------------------------------------------------

    async def ws_connect(self, path: str, query_string: str) -> aiohttp.ClientWebSocketResponse:
        """Open a WebSocket to the recorder with the admin bearer (talk-down)."""
        token = await self.ensure_token()
        try:
            return await self._session.ws_connect(
                self.url(path, query_string),
                headers={"Authorization": f"Bearer {token}"},
                ssl=self._ssl,
                heartbeat=30,
            )
        except aiohttp.ClientSSLError as err:
            raise AuviCertError(f"TLS failure: {err}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            raise AuviConnectionError(f"websocket {path}: {err}") from err

    async def proxy_request(
        self,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        data: Any,
    ) -> ClientResponse:
        """Forward a browser request from the sidebar panel.

        No redirects are followed (the Location header is rewritten by the
        caller), bodies are never decompressed (the upstream is asked for
        identity encoding) and there is no total timeout: event streams and
        clip downloads run as long as the browser keeps reading.
        """
        return await self.send(
            method, path, query_string=query_string, headers=headers, data=data,
            timeout=ClientTimeout(total=None, sock_connect=10, sock_read=None),
            auto_decompress=False,
        )

    # ---- rules (ADR-0543) ---------------------------------------------------

    async def get_rules(self) -> dict[str, Any]:
        """The recorder's rules document: rules of every origin, secrets, vocabularies."""
        return await self.api_json("GET", "/api/v1/local/rules")

    async def create_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Create a recorder-local rule; returns it with its id."""
        return await self.api_json("POST", "/api/v1/local/rules", json=rule)

    async def update_rule(self, rule_id: str, rule: dict[str, Any]) -> dict[str, Any]:
        """Replace a recorder-local rule."""
        return await self.api_json("PUT", f"/api/v1/local/rules/{rule_id}", json=rule)

    async def delete_rule(self, rule_id: str) -> None:
        """Remove a recorder-local rule (already gone is fine)."""
        resp = await self.send("DELETE", f"/api/v1/local/rules/{rule_id}")
        try:
            if resp.status == 401:
                raise AuviAuthError("the recorder refused the admin session")
            if resp.status >= 400 and resp.status != 404:
                text = (await resp.text()).strip()
                raise AuviConnectionError(f"DELETE rule {rule_id}: HTTP {resp.status} {text[:200]}")
        finally:
            resp.release()

    async def set_secret(self, name: str, value: str) -> None:
        """Store a webhook secret in the recorder keystore (value never echoed)."""
        await self.api_json("PUT", f"/api/v1/local/secrets/{name}", json={"value": value})

    async def delete_secret(self, name: str) -> None:
        """Remove a webhook secret (already gone is fine)."""
        resp = await self.send("DELETE", f"/api/v1/local/secrets/{name}")
        try:
            if resp.status == 401:
                raise AuviAuthError("the recorder refused the admin session")
            if resp.status >= 400 and resp.status != 404:
                text = (await resp.text()).strip()
                raise AuviConnectionError(f"DELETE secret {name}: HTTP {resp.status} {text[:200]}")
        finally:
            resp.release()
