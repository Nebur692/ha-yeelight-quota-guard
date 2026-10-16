"""Setup: get each bulb's miIO token, from the Xiaomi cloud or by hand.

Nothing is stored until it has been checked: a cloud token is only kept for a
bulb that the core Yeelight integration knows (matched by device id), and a
hand-typed token only after the bulb itself has answered with it.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from . import burburito
from .cloud import (
    CloudAuthError,
    CloudCodeError,
    CloudError,
    CloudUnexpected,
    CloudVerificationRequired,
    LoginStep,
    XiaomiLogin,
    yeelight_tokens,
)
from .const import (
    CONF_BULBS,
    CONF_COUNTRY,
    CONF_TOKEN,
    COUNTRIES,
    DEFAULT_OPTIONS,
    DOMAIN,
    OPT_ONLY_WHEN_OFF,
    OPT_PREVENT,
    OPT_PREVENT_AFTER_DAYS,
    OPT_REPAIR,
    OPT_SCAN_MINUTES,
    OPT_WINDOW_END,
    OPT_WINDOW_START,
)
from .miio import MiioDevice, MiioError, MiioTokenError, MiioWrongDevice, parse_token
from .yeelight_link import yeelight_bulbs

_LOGGER = logging.getLogger(__name__)
SIGNED_IN = LoginStep(signed_in=True)

TITLE = "Yeelight Quota Guard"
CONF_BULB = "bulb"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CODE = "code"
CONF_WATCH = "watch"


class _TokenSteps:
    """The token steps, shared by the setup and the options flow."""

    hass: HomeAssistant
    _bulbs: dict[str, dict[str, str]]
    _found: list[SelectOptionDict]
    _without: list[str]
    _cloud_tokens: dict[str, str]
    _total: int = 0
    _login: XiaomiLogin
    _country: str = "de"
    _captcha: str = ""
    _sent_to: str = ""

    def _err(self, key: str, error: str, extra: dict[str, str] | None = None) -> tuple[dict, dict]:
        placeholders = burburito.footer(key, self.hass.config.language)
        return {"base": error}, {**placeholders, **(extra or {})}

    async def _cloud(self, user_input: dict[str, Any] | None, step_id: str) -> ConfigFlowResult | None:
        if user_input is not None:
            self._country = user_input[CONF_COUNTRY]
            login = getattr(self, "_login", None)
            if login is not None and login.signed_in and login.username == user_input[CONF_USERNAME]:
                # Same account, another server (after "no bulbs"): no need to go
                # through the captcha and the email code again.
                return await self._login_step(lambda: SIGNED_IN, step_id)
            self._login = XiaomiLogin(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            return await self._login_step(self._login.start, step_id)
        return self._cloud_form(step_id, {}, {"url": "", "code": "", "joke": ""})

    async def _cloud_captcha(self, user_input: dict[str, Any] | None) -> ConfigFlowResult | None:
        return await self._login_step(lambda: self._login.captcha(user_input[CONF_CODE]), "cloud")

    async def _cloud_code(self, user_input: dict[str, Any] | None) -> ConfigFlowResult | None:
        return await self._login_step(lambda: self._login.verify(user_input[CONF_CODE]), "cloud")

    async def _login_step(self, call: Callable[[], LoginStep], step_id: str) -> ConfigFlowResult | None:
        """Run one sign-in step; ask for what Xiaomi wants next, or read the tokens."""
        try:
            step = await self.hass.async_add_executor_job(call)
            if step.captcha is not None:
                return self._code_form("cloud_captcha", step.captcha_was_wrong, image=step.captcha)
            if step.code_sent_to is not None:
                self._sent_to = step.code_sent_to
                return self._code_form("cloud_code", False)
            devices = await self.hass.async_add_executor_job(self._login.devices, self._country)
        except CloudCodeError:
            return self._code_form("cloud_code", True)
        except CloudVerificationRequired as err:
            return self._cloud_form(
                step_id,
                *self._err("configuracion/verificar-identidad/rechazo-remoto", "verification", {"url": err.url}),
            )
        except CloudAuthError:
            return self._cloud_form(
                step_id,
                *self._err("configuracion/verificar-identidad/credencial-invalida", "invalid_auth", {"url": ""}),
            )
        except CloudUnexpected as err:
            _LOGGER.warning("%s", err)
            return self._cloud_form(
                step_id,
                *self._err("configuracion/verificar-identidad/respuesta-ilegible", "cloud_refused", {"url": ""}),
            )
        except CloudError as err:
            _LOGGER.warning("Xiaomi cloud: %s", err)
            return self._cloud_form(
                step_id, *self._err("configuracion/verificar-identidad/sin-conexion", "cannot_connect", {"url": ""})
            )
        except Exception:
            # Whatever the cloud throws next, the person gets a message and a code, never "Unknown error".
            _LOGGER.exception("Unexpected failure signing in to Xiaomi")
            return self._cloud_form(
                step_id,
                *self._err("configuracion/verificar-identidad/respuesta-ilegible", "cloud_refused", {"url": ""}),
            )
        cloud = yeelight_tokens(devices)
        links = yeelight_bulbs(self.hass)
        matched = {did: bulb for did, bulb in cloud.items() if did in links}
        if not matched:
            return self._cloud_form(
                step_id, *self._err("configuracion/cambiar-ajustes/no-encontrado", "no_bulbs", {"url": ""})
            )
        self._cloud_tokens = {str(did): bulb.token for did, bulb in matched.items()}
        self._found = [
            SelectOptionDict(value=str(did), label=links[did].name)
            for did in sorted(matched, key=lambda did: links[did].name.lower())
        ]
        self._without = sorted(
            (link.name for did, link in links.items() if did not in matched and str(did) not in self._bulbs),
            key=str.lower,
        )
        self._total = len(links)
        return None

    def _code_form(self, step_id: str, wrong: bool, image: bytes | None = None) -> ConfigFlowResult:
        if image is not None:
            kind = "png" if image.startswith(b"\x89PNG") else "jpeg"
            self._captcha = f"data:image/{kind};base64," + base64.b64encode(image).decode()
        errors: dict[str, str] = {}
        placeholders = {"code": "", "joke": ""}
        if wrong:
            key, error = (
                ("configuracion/verificar-identidad/dato-invalido", "captcha_wrong")
                if step_id == "cloud_captcha"
                else ("configuracion/verificar-identidad/codigo-caducado", "code_wrong")
            )
            errors, placeholders = self._err(key, error)
        placeholders = {**placeholders, "image": self._captcha, "address": self._sent_to}
        schema = vol.Schema({vol.Required(CONF_CODE): TextSelector(TextSelectorConfig(autocomplete="one-time-code"))})
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id, data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    def _cloud_form(self, step_id: str, errors: dict, placeholders: dict) -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                ),
                vol.Required(CONF_COUNTRY, default="de"): SelectSelector(
                    SelectSelectorConfig(
                        options=COUNTRIES, translation_key=CONF_COUNTRY, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        placeholders = {"url": "", "code": "", "joke": "", **placeholders}
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id, data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    async def async_step_cloud_captcha(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := await self._cloud_captcha(user_input)) is not None:
            return form
        return await self.async_step_cloud_done()  # type: ignore[attr-defined]

    async def async_step_cloud_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := await self._cloud_code(user_input)) is not None:
            return form
        return await self.async_step_cloud_done()  # type: ignore[attr-defined]

    async def _manual(self, user_input: dict[str, Any] | None, step_id: str) -> ConfigFlowResult | None:
        links = yeelight_bulbs(self.hass)
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"code": "", "joke": ""}
        if user_input is not None:
            link = links.get(int(user_input[CONF_BULB]))
            token = user_input[CONF_TOKEN].strip().lower()
            try:
                parse_token(token)
            except ValueError:
                errors, placeholders = self._err("configuracion/cambiar-ajustes/dato-invalido", "invalid_token")
            else:
                if link is None or not link.host:
                    errors, placeholders = self._err(
                        "configuracion/cambiar-ajustes/dispositivo-mudo", "cannot_reach_bulb"
                    )
                else:
                    try:
                        await MiioDevice(link.host, token, link.did).info()
                    except MiioWrongDevice:
                        errors, placeholders = self._err(
                            "configuracion/cambiar-ajustes/conflicto-estado", "wrong_device"
                        )
                    except MiioTokenError:
                        errors, placeholders = self._err(
                            "configuracion/cambiar-ajustes/credencial-invalida", "wrong_token"
                        )
                    except MiioError:
                        errors, placeholders = self._err(
                            "configuracion/cambiar-ajustes/dispositivo-mudo", "cannot_reach_bulb"
                        )
                    else:
                        self._bulbs[str(link.did)] = {CONF_TOKEN: token}
                        return None
        options = [
            SelectOptionDict(value=str(did), label=f"{link.name} ({link.host or '?'})")
            for did, link in sorted(links.items(), key=lambda item: item[1].name.lower())
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_BULB): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id, data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    def _cloud_done(self, user_input: dict[str, Any] | None) -> ConfigFlowResult | None:
        """Tick the bulbs to guard: a native checkbox list, one line per bulb, all ticked."""
        errors: dict[str, str] = {}
        placeholders = {"code": "", "joke": ""}
        if user_input is not None:
            chosen = set(user_input.get(CONF_WATCH) or [])
            if chosen:
                for did, token in self._cloud_tokens.items():
                    if did in chosen:
                        self._bulbs[did] = {CONF_TOKEN: token}
                    else:
                        self._bulbs.pop(did, None)
                return None
            errors, placeholders = self._err("configuracion/cambiar-ajustes/falta-config", "nothing_chosen")
        schema = vol.Schema(
            {
                vol.Optional(CONF_WATCH, default=[o["value"] for o in self._found]): SelectSelector(
                    SelectSelectorConfig(options=self._found, multiple=True, mode=SelectSelectorMode.LIST)
                ),
            }
        )
        placeholders = {
            **placeholders,
            "found": str(len(self._found)),
            "total": str(self._total),
            "without_count": str(len(self._without)),
            "without": "".join(f"\n- {name}" for name in self._without),
        }
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="cloud_done", data_schema=schema, errors=errors, description_placeholders=placeholders
        )


class YeelightQuotaGuardConfigFlow(_TokenSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._bulbs = {}
        self._found = []
        self._without = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GuardOptionsFlow:
        return GuardOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not yeelight_bulbs(self.hass):
            return self.async_abort(reason="no_yeelight")
        return self.async_show_menu(step_id="user", menu_options=["cloud", "manual"])

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := await self._cloud(user_input, "cloud")) is not None:
            return form
        return await self.async_step_cloud_done()

    async def async_step_cloud_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := self._cloud_done(user_input)) is not None:
            return form
        return self._create()

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := await self._manual(user_input, "manual")) is not None:
            return form
        return await self.async_step_manual_more()

    async def async_step_manual_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="manual_more", menu_options=["manual", "finish"])

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self._create()

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(title=TITLE, data={CONF_BULBS: self._bulbs}, options=dict(DEFAULT_OPTIONS))


class GuardOptionsFlow(_TokenSteps, OptionsFlowWithReload):
    def __init__(self) -> None:
        self._bulbs = {}
        self._found = []
        self._without = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._bulbs = {k: dict(v) for k, v in self.config_entry.data.get(CONF_BULBS, {}).items()}
        return self.async_show_menu(step_id="init", menu_options=["settings", "manual", "cloud"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[OPT_PREVENT_AFTER_DAYS] = int(user_input[OPT_PREVENT_AFTER_DAYS])
            user_input[OPT_SCAN_MINUTES] = int(user_input[OPT_SCAN_MINUTES])
            return self.async_create_entry(data=user_input)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(OPT_REPAIR, default=current[OPT_REPAIR]): BooleanSelector(),
                vol.Required(OPT_PREVENT, default=current[OPT_PREVENT]): BooleanSelector(),
                vol.Required(OPT_PREVENT_AFTER_DAYS, default=current[OPT_PREVENT_AFTER_DAYS]): NumberSelector(
                    NumberSelectorConfig(min=7, max=49, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(OPT_WINDOW_START, default=current[OPT_WINDOW_START]): TimeSelector(),
                vol.Required(OPT_WINDOW_END, default=current[OPT_WINDOW_END]): TimeSelector(),
                vol.Required(OPT_ONLY_WHEN_OFF, default=current[OPT_ONLY_WHEN_OFF]): BooleanSelector(),
                vol.Required(OPT_SCAN_MINUTES, default=current[OPT_SCAN_MINUTES]): NumberSelector(
                    NumberSelectorConfig(min=5, max=120, step=5, mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := await self._manual(user_input, "manual")) is not None:
            return form
        return self._save()

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := await self._cloud(user_input, "cloud")) is not None:
            return form
        return await self.async_step_cloud_done()

    async def async_step_cloud_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (form := self._cloud_done(user_input)) is not None:
            return form
        return self._save()

    def _save(self) -> ConfigFlowResult:
        changed = self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_BULBS: self._bulbs}
        )
        if changed:
            # Home Assistant only reloads by itself when the options change;
            # new tokens need it too, for their bulbs' button and sensors.
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(data=dict(self.config_entry.options))
