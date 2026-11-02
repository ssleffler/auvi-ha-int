"""Config flow: host, admin password, trust the recorder's root once."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import ssl
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AuviAuthError,
    AuviClient,
    AuviConnectionError,
    AuviNoPasswordError,
    build_ssl_context,
    fetch_recorder_cert,
)
from .const import (
    CONF_CA_FINGERPRINT,
    CONF_CA_PEM,
    CONF_PANEL_REQUIRE_ADMIN,
    CONF_RTSP_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_STREAM_CLASS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STREAM_CLASS,
    DEFAULT_USERNAME,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    STREAM_CLASSES,
)

LOGGER = logging.getLogger(__package__)

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
STREAM_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=STREAM_CLASSES, translation_key="stream_class",
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


def _user_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Optional(CONF_RTSP_PASSWORD, default=""): PASSWORD_SELECTOR,
            vol.Required(CONF_STREAM_CLASS, default=DEFAULT_STREAM_CLASS): STREAM_SELECTOR,
        }
    )


async def _validate_login(
    hass: HomeAssistant, host: str, port: int, username: str, password: str, ca_pem: str
) -> dict[str, Any]:
    """Log in with the pinned root and return the recorder's TLS/identity doc."""
    ctx = await hass.async_add_executor_job(build_ssl_context, ca_pem)
    client = AuviClient(async_get_clientsession(hass), host, port, username, password, ctx)
    await client.login()
    return await client.get_tls()


class AuviConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup, reauth and reconfigure flows."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._pending: dict[str, Any] = {}
        self._ca_pem = ""
        self._fingerprint = ""
        self._mode = "user"

    # ---- helpers -----------------------------------------------------------

    async def _fetch_cert(self, host: str, port: int) -> str | None:
        """Fetch the root; return an error key or None."""
        try:
            self._ca_pem, self._fingerprint = await fetch_recorder_cert(
                async_get_clientsession(hass=self.hass), host, port
            )
        except AuviConnectionError as err:
            LOGGER.debug("certificate fetch failed: %s", err)
            return "cannot_connect"
        return None

    async def _finish(self, errors: dict[str, str]) -> ConfigFlowResult | None:
        """Log in with the trusted root and create/update the entry."""
        p = self._pending
        try:
            tls = await _validate_login(
                self.hass, p[CONF_HOST], p[CONF_PORT], p[CONF_USERNAME], p[CONF_PASSWORD],
                self._ca_pem,
            )
        except AuviNoPasswordError:
            errors["base"] = "no_password_set"
            return None
        except AuviAuthError:
            errors["base"] = "invalid_auth"
            return None
        except AuviConnectionError as err:
            LOGGER.debug("login failed: %s", err)
            errors["base"] = "cannot_connect"
            return None
        except (ssl.SSLError, ValueError):
            errors["base"] = "cert_error"
            return None

        hostname = str(tls.get("hostname") or f"{p[CONF_HOST]}:{p[CONF_PORT]}")
        data = {
            CONF_HOST: p[CONF_HOST],
            CONF_PORT: p[CONF_PORT],
            CONF_USERNAME: p[CONF_USERNAME],
            CONF_PASSWORD: p[CONF_PASSWORD],
            CONF_CA_PEM: self._ca_pem,
            CONF_CA_FINGERPRINT: self._fingerprint,
        }
        if self._mode == "reauth":
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data_updates=data)
        if self._mode == "reconfigure":
            entry = self._get_reconfigure_entry()
            await self.async_set_unique_id(hostname)
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(entry, data_updates=data)

        await self.async_set_unique_id(hostname)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: p[CONF_HOST], CONF_PORT: p[CONF_PORT]}
        )
        options = {
            CONF_RTSP_PASSWORD: p.get(CONF_RTSP_PASSWORD, ""),
            CONF_STREAM_CLASS: p.get(CONF_STREAM_CLASS, DEFAULT_STREAM_CLASS),
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_PANEL_REQUIRE_ADMIN: False,
        }
        return self.async_create_entry(title=f"Auvi NVR {hostname}", data=data, options=options)

    # ---- user ---------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the recorder and the admin password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._mode = "user"
            self._pending = dict(user_input)
            err = await self._fetch_cert(user_input[CONF_HOST], int(user_input[CONF_PORT]))
            if err is None:
                return await self.async_step_trust_cert()
            errors["base"] = err
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(self._pending), errors=errors
        )

    async def async_step_trust_cert(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the root's fingerprint; the user confirms it once."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._finish(errors)
            if result is not None:
                return result
            if errors.get("base") in ("invalid_auth", "no_password_set"):
                step = {"reauth": "reauth_confirm", "reconfigure": "reconfigure"}.get(
                    self._mode, "user"
                )
                return self.async_show_form(
                    step_id=step,
                    data_schema=self._schema_for(step),
                    errors=errors,
                    description_placeholders={"host": str(self._pending.get(CONF_HOST, ""))},
                )
        return self.async_show_form(
            step_id="trust_cert",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "host": str(self._pending.get(CONF_HOST, "")),
                "fingerprint": self._fingerprint,
            },
        )

    def _schema_for(self, step: str) -> vol.Schema:
        if step == "reauth_confirm":
            return vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})
        if step == "reconfigure":
            return vol.Schema(
                {
                    vol.Required(CONF_HOST, default=self._pending.get(CONF_HOST, "")): str,
                    vol.Required(
                        CONF_PORT, default=self._pending.get(CONF_PORT, DEFAULT_PORT)
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            )
        return _user_schema(self._pending)

    # ---- reauth -------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The stored password or certificate stopped working."""
        self._mode = "reauth"
        self._pending = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again; re-trust the root if it changed."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            self._pending[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            err = await self._fetch_cert(
                self._pending[CONF_HOST], int(self._pending.get(CONF_PORT, DEFAULT_PORT))
            )
            if err is not None:
                errors["base"] = err
            elif self._fingerprint != entry.data.get(CONF_CA_FINGERPRINT):
                return await self.async_step_trust_cert()
            else:
                result = await self._finish(errors)
                if result is not None:
                    return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema_for("reauth_confirm"),
            errors=errors,
            description_placeholders={"host": str(self._pending.get(CONF_HOST, ""))},
        )

    # ---- reconfigure --------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host or port (and password) of an existing recorder."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if not self._pending:
            self._pending = dict(entry.data)
        self._mode = "reconfigure"
        if user_input is not None:
            self._pending.update(user_input)
            err = await self._fetch_cert(user_input[CONF_HOST], int(user_input[CONF_PORT]))
            if err is None:
                return await self.async_step_trust_cert()
            errors["base"] = err
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema_for("reconfigure"), errors=errors
        )

    # ---- options ------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AuviOptionsFlow:
        """Options: polling, RTSP password, stream class, panel access."""
        return AuviOptionsFlow()


class AuviOptionsFlow(OptionsFlowWithReload):
    """Options editable after setup; the entry reloads when they change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                ),
                vol.Optional(CONF_RTSP_PASSWORD, default=""): PASSWORD_SELECTOR,
                vol.Required(CONF_STREAM_CLASS, default=DEFAULT_STREAM_CLASS): STREAM_SELECTOR,
                vol.Required(CONF_PANEL_REQUIRE_ADMIN, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )
