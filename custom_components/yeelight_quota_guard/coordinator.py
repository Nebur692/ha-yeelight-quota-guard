"""Polls every guarded bulb over miIO and restarts the ones that need it."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import burburito
from .const import (
    CONF_BULBS,
    CONF_TOKEN,
    DEFAULT_OPTIONS,
    DOMAIN,
    EVENT_RESTARTED,
    OPT_ONLY_WHEN_OFF,
    OPT_PREVENT,
    OPT_PREVENT_AFTER_DAYS,
    OPT_REPAIR,
    OPT_SCAN_MINUTES,
    OPT_WINDOW_END,
    OPT_WINDOW_START,
    RESTART_POLL_SECONDS,
    RESTART_WAIT,
    TOKEN_FAILURES_BEFORE_ISSUE,
)
from .guard import (
    IMMUNE_AFTER_S,
    QUOTA_LIMIT_S,
    Reason,
    Settings,
    Snapshot,
    decide,
    powered_on_at,
    stable_boot_time,
)
from .lan import LanAnswer, probe
from .miio import MiioDevice, MiioError, MiioTimeout, MiioTokenError, MiioWrongDevice
from .yeelight_link import YeelightBulb, yeelight_bulbs

_LOGGER = logging.getLogger(__name__)
README = "https://github.com/Nebur692/ha-yeelight-quota-guard#readme"

type GuardConfigEntry = ConfigEntry[GuardCoordinator]


# Two polls in a row (30 min by default) before an unavailable light counts as locked.
UNAVAILABLE_POLLS_FOR_LOCK = 2
# Same for a core entry stuck asking to power cycle a bulb below the limit.
STUCK_POLLS_FOR_HANG = 2


@dataclass
class BulbState:
    did: int
    name: str
    host: str | None = None
    reachable: bool = False
    life_s: int | None = None
    power: str | None = None
    firmware: str | None = None
    model: str | None = None
    powered_on: datetime | None = None
    checked: datetime | None = None
    token_failures: int = 0
    unavailable_polls: int = 0
    stuck: bool = False
    stuck_polls: int = 0
    lan: LanAnswer | None = None
    # Seen working past 2^32 ms on this firmware: the lock does not affect it.
    immune: bool = False
    last_restart: datetime | None = None
    last_result: str | None = None


# Bulbs asked at the same time: enough to keep a poll short with many bulbs,
# few enough not to flood a busy 2.4 GHz network.
PARALLEL_BULBS = 4


class RestartError(Exception):
    """A restart that did not happen; carries the burburito key."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


def settings_from(options: dict[str, Any]) -> Settings:
    merged = {**DEFAULT_OPTIONS, **options}
    return Settings(
        repair=bool(merged[OPT_REPAIR]),
        prevent=bool(merged[OPT_PREVENT]),
        prevent_after_days=int(merged[OPT_PREVENT_AFTER_DAYS]),
        window_start=dt_util.parse_time(str(merged[OPT_WINDOW_START])),
        window_end=dt_util.parse_time(str(merged[OPT_WINDOW_END])),
        only_when_off=bool(merged[OPT_ONLY_WHEN_OFF]),
    )


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


@callback
def clear_issues(hass: HomeAssistant) -> None:
    """Every issue this integration may have raised."""
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


class GuardCoordinator(DataUpdateCoordinator[dict[int, BulbState]]):
    config_entry: GuardConfigEntry

    def __init__(self, hass: HomeAssistant, entry: GuardConfigEntry) -> None:
        minutes = int({**DEFAULT_OPTIONS, **entry.options}[OPT_SCAN_MINUTES])
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )
        self.settings = settings_from(dict(entry.options))
        self._states: dict[int, BulbState] = {}
        self._lock = asyncio.Lock()
        self._acting = False
        # What has to survive a restart of Home Assistant:
        # - immune: {device id: firmware} of bulbs seen working past the limit.
        #   Per firmware: an update could bring the bug, so it is learnt again.
        # - last_restart: {device id: ISO time}, so MIN_GAP_BETWEEN_RESTARTS
        #   holds across restarts and a bulb that ignores the order is not
        #   restarted again at every start.
        self._store: Store[dict[str, dict[str, str]]] = Store(hass, 1, store_key(entry.entry_id))
        self._immune: dict[str, str] = {}
        self._last_restart: dict[str, str] = {}
        self._guarded: set[int] | None = None
        self._parallel = asyncio.Semaphore(PARALLEL_BULBS)

    async def _async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self._immune = dict(stored.get("immune") or {})
        self._last_restart = dict(stored.get("last_restart") or {})

    async def _save(self) -> None:
        await self._store.async_save({"immune": self._immune, "last_restart": self._last_restart})

    # ---- configuration -------------------------------------------------

    @property
    def tokens(self) -> dict[int, str]:
        return {int(did): bulb[CONF_TOKEN] for did, bulb in self.config_entry.data.get(CONF_BULBS, {}).items()}

    def links(self) -> dict[int, YeelightBulb]:
        return yeelight_bulbs(self.hass)

    def _language(self) -> str:
        return self.hass.config.language or "en"

    # ---- polling -------------------------------------------------------

    async def _async_update_data(self) -> dict[int, BulbState]:
        links = self.links()
        tokens = self.tokens
        now = dt_util.now()
        # Guarded: a token, and a Yeelight entry that exists and is enabled.
        guarded = {did for did in tokens if did in links}
        for did in set(self._states) - guarded:
            # Its token was taken out, or its Yeelight entry removed or disabled.
            del self._states[did]
        if self._guarded is None:
            self._guarded = guarded
        elif guarded != self._guarded:
            # Entities are made at setup: reload so bulbs that came or went get
            # (or lose) their button and sensors.
            _LOGGER.info("The guarded bulbs changed; reloading")
            self._guarded = guarded
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        await asyncio.gather(*(self._check(did, tokens[did], links[did], now) for did in sorted(guarded)))
        self._sync_issues(links)
        decisions = self._decisions(now)
        if decisions and not self._acting:
            self._acting = True
            self.config_entry.async_create_background_task(self.hass, self._act(decisions), f"{DOMAIN} restarts")
        return dict(self._states)

    async def _check(self, did: int, token: str, link: YeelightBulb, now: datetime) -> None:
        async with self._parallel:
            state = self._states.setdefault(did, BulbState(did=did, name=link.name))
            state.name = link.name
            state.host = link.host or state.host
            if state.last_restart is None and (stored := self._last_restart.get(str(did))):
                state.last_restart = dt_util.parse_datetime(stored)
            try:
                await self._poll(state, token, now)
                self._watch_core(state, link)
                await self._ask_lan(state)
            except Exception:
                # One bulb answering something odd must not take the others down.
                _LOGGER.exception("Unexpected failure checking %s", state.name)
                state.reachable = False
            self._clear_restart_failed(state)

    def _clear_restart_failed(self, state: BulbState) -> None:
        """The person power-cycled it, as the issue said: it booted after the failed try."""
        issue_id = f"restart_failed_{state.did}"
        if (
            state.reachable
            and state.powered_on is not None
            and state.last_restart is not None
            and state.powered_on > state.last_restart
            and ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id) is not None
        ):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _poll(self, state: BulbState, token: str, now: datetime) -> None:
        state.checked = now
        if not state.host:
            state.reachable = False
            return
        device = MiioDevice(state.host, token, state.did)
        try:
            info = await device.info()
            power = await device.power()
        except MiioTokenError:
            state.reachable = False
            state.token_failures += 1
            return
        except MiioWrongDevice as err:
            # The address now belongs to something else; the core Yeelight
            # integration updates it when it finds the bulb again.
            _LOGGER.debug("%s: %s", state.name, err)
            state.reachable = False
            return
        except MiioError as err:
            # Off at the wall switch, or Wi-Fi: not our business.
            _LOGGER.debug("%s did not answer: %s", state.name, err)
            state.reachable = False
            return
        state.reachable = True
        state.token_failures = 0
        state.power = power
        state.firmware = info.get("fw_ver")
        state.model = info.get("model")
        life = info.get("life")
        if not isinstance(life, int) or life < 0:
            # No uptime, nothing to decide on: better than taking it as 0 forever.
            state.life_s = None
            return
        state.life_s = life
        state.powered_on = stable_boot_time(state.powered_on, powered_on_at(now, life))

    def _watch_core(self, state: BulbState, link: YeelightBulb | None) -> None:
        """How the core Yeelight integration is doing with this bulb.

        Stuck: its entry retries with "try power cycling" (what 16 locked bulbs
        showed on 15-10-2026). Unavailable: its light is unavailable while the
        bulb answers us over miIO, counted in polls so a Wi-Fi blip is not one.
        """
        state.stuck = bool(link and link.stuck)
        state.stuck_polls = state.stuck_polls + 1 if state.stuck and state.reachable else 0
        lights = self._core_lights(link)
        if state.reachable and lights and all(self._unavailable(entity_id) for entity_id in lights):
            state.unavailable_polls += 1
        else:
            state.unavailable_polls = 0

    async def _ask_lan(self, state: BulbState) -> None:
        """Past the limit, or stuck below it, ask the bulb the way Home Assistant does."""
        state.lan = None
        if (
            not state.reachable
            or not state.host
            or state.life_s is None
            or (state.life_s < QUOTA_LIMIT_S and state.stuck_polls < STUCK_POLLS_FOR_HANG)
        ):
            state.immune = self._is_immune(state)
            return
        state.lan = await probe(state.host)
        if (
            state.lan is LanAnswer.WORKS
            and state.life_s >= IMMUNE_AFTER_S
            and state.firmware
            and self._immune.get(str(state.did)) != state.firmware
        ):
            self._immune[str(state.did)] = state.firmware
            _LOGGER.info("%s works past the 49.7-day limit on firmware %s: not affected", state.name, state.firmware)
            await self._save()
        state.immune = self._is_immune(state)

    def _is_immune(self, state: BulbState) -> bool:
        return bool(state.firmware) and self._immune.get(str(state.did)) == state.firmware

    def _core_lights(self, link: YeelightBulb | None) -> list[str]:
        if link is None:
            return []
        registry = er.async_get(self.hass)
        return [
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, link.entry_id)
            if e.domain == "light" and not e.disabled
        ]

    def _unavailable(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_UNAVAILABLE

    def _decide(self, state: BulbState, now: datetime, power: str | None) -> Reason | None:
        locked = state.stuck or state.lan is LanAnswer.LOCKED or state.unavailable_polls >= UNAVAILABLE_POLLS_FOR_LOCK
        # A bulb that still answers over LAN is left to the core integration's own retries.
        hung = state.stuck_polls >= STUCK_POLLS_FOR_HANG and state.lan in (LanAnswer.UNKNOWN, LanAnswer.LOCKED)
        snapshot = Snapshot(state.life_s, power, state.last_restart, locked, hung, state.immune)
        return decide(snapshot, self.settings, now)

    def _decisions(self, now: datetime) -> list[tuple[int, Reason]]:
        decisions = []
        for did, state in self._states.items():
            if state.reachable and (reason := self._decide(state, now, state.power)) is not None:
                decisions.append((did, reason))
        return decisions

    async def _act(self, decisions: list[tuple[int, Reason]]) -> None:
        try:
            for did, reason in decisions:
                # Restarts go one after another, a minute or more each: what was
                # true at the poll (the window, the light off, no recent
                # restart) is checked again right before each one.
                if not await self._still_needed(did, reason):
                    continue
                try:
                    await self.async_restart(did, reason)
                except RestartError as err:
                    _LOGGER.warning("%s (burburito %s)", err, burburito.code(err.key))
        finally:
            self._acting = False

    async def _still_needed(self, did: int, reason: Reason) -> bool:
        state = self._states.get(did)
        token = self.tokens.get(did)
        if state is None or token is None or not state.host:
            return False
        try:
            power = await MiioDevice(state.host, token, did).power()
        except MiioError:
            return False
        return self._decide(state, dt_util.now(), power) == reason

    # ---- restarting ----------------------------------------------------

    async def async_restart(self, did: int, reason: Reason) -> None:
        """Restart one bulb, wait for it, put its power back as it was."""
        async with self._lock:
            state = self._states.get(did)
            token = self.tokens.get(did)
            if state is None or token is None or not state.host:
                raise RestartError(
                    "guardia/accionar-dispositivo/dispositivo-mudo",
                    f"Bulb {did} is not known yet",
                )
            device = MiioDevice(state.host, token, did)
            try:
                before = await device.power()
            except MiioError as err:
                self._record(state, reason, "no_answer")
                raise RestartError(
                    "guardia/accionar-dispositivo/dispositivo-mudo",
                    f"{state.name} did not answer: {err}",
                ) from err
            # Counted from the moment the order goes out, and kept across
            # restarts of Home Assistant: a bulb that ignores it must wait out
            # MIN_GAP_BETWEEN_RESTARTS, not be ordered again every poll.
            state.last_restart = dt_util.now()
            self._last_restart[str(did)] = state.last_restart.isoformat()
            await self._save()
            try:
                await device.reboot()
            except (MiioTimeout, MiioTokenError):
                # No reply can mean it restarted before answering: the uptime
                # counter below decides whether it did.
                _LOGGER.debug("%s did not confirm the restart; checking its uptime", state.name)
            except MiioError as err:
                self._record(state, reason, "no_answer")
                raise RestartError(
                    "guardia/accionar-dispositivo/dispositivo-mudo",
                    f"{state.name} did not accept the restart: {err}",
                ) from err

            life = await self._wait_until_back(state, token)
            if life is None:
                self._record(state, reason, "not_back")
                self._issue_restart_failed(state)
                raise RestartError(
                    "guardia/vigilar/firmware-colgado",
                    f"{state.name} did not come back after the restart",
                )

            now = dt_util.now()
            state.life_s, state.reachable = life, True
            state.powered_on = powered_on_at(now, life).replace(microsecond=0)
            state.lan = None
            ir.async_delete_issue(self.hass, DOMAIN, f"restart_failed_{did}")

            result = "ok"
            device = MiioDevice(state.host, token, did)
            try:
                after = await device.power()
                if before in ("on", "off") and after != before:
                    await device.set_power(before)
                    after = await device.power()
                    result = "power_restored" if after == before else "power_changed"
                state.power = after
            except MiioError:
                result = "power_unknown"
            if result == "power_changed":
                _LOGGER.warning(
                    "%s came back %s after the restart and could not be set back to %s (burburito %s)",
                    state.name,
                    state.power,
                    before,
                    burburito.code("guardia/accionar-dispositivo/conflicto-estado"),
                )

            self._reload_core_entry(did)
            self._record(state, reason, result)
            _LOGGER.info("Restarted %s (%s): %s", state.name, reason, result)
            self.async_update_listeners()

    async def _wait_until_back(self, state: BulbState, token: str) -> int | None:
        """Seconds of uptime once the bulb answers with a fresh counter."""
        deadline = dt_util.utcnow() + RESTART_WAIT
        while dt_util.utcnow() < deadline:
            await asyncio.sleep(RESTART_POLL_SECONDS)
            try:
                info = await MiioDevice(state.host or "", token, state.did, timeout=3).info()
            except MiioError:
                continue
            life = int(info.get("life") or 0)
            if life < RESTART_WAIT.total_seconds() * 2:
                return life
            # Still the old counter: it has not gone down yet. Keep waiting;
            # only the deadline says the restart did not happen.
        return None

    def _reload_core_entry(self, did: int) -> None:
        """Skip the core integration's retry back-off once the bulb is fine.

        Scheduled, not awaited: the core asks integrations to do it this way,
        and it cannot raise if the entry is removed meanwhile."""
        link = self.links().get(did)
        if link is None or link.state not in (
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.SETUP_ERROR,
        ):
            return
        self.hass.config_entries.async_schedule_reload(link.entry_id)

    def _record(self, state: BulbState, reason: Reason, result: str) -> None:
        state.last_result = result
        self.hass.bus.async_fire(
            EVENT_RESTARTED,
            {"bulb": state.name, "did": state.did, "reason": str(reason), "result": result},
        )

    # ---- repairs -------------------------------------------------------

    def _footer(self, key: str) -> dict[str, str]:
        return burburito.footer(key, self._language())

    def _raise_issue(
        self, issue_id: str, translation_key: str, severity: ir.IssueSeverity, key: str, **data: str
    ) -> None:
        """Create the issue unless it is already there saying the same: the joke
        is picked at random, so writing it every poll would rewrite the
        registry and change the text under the person's eyes."""
        existing = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
        if (
            existing is not None
            and existing.translation_placeholders is not None
            and all(existing.translation_placeholders.get(k) == v for k, v in data.items())
        ):
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders={**data, **self._footer(key)},
            learn_more_url=README,
        )

    def _sync_issues(self, links: dict[int, YeelightBulb]) -> None:
        tokens = self.tokens
        missing = sorted(link.name for did, link in links.items() if link.stuck and did not in tokens)
        if missing:
            self._raise_issue(
                "missing_token",
                "missing_token",
                ir.IssueSeverity.WARNING,
                "guardia/vigilar/falta-config",
                bulbs=", ".join(missing),
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "missing_token")

        for did, state in self._states.items():
            issue_id = f"token_rejected_{did}"
            if state.token_failures >= TOKEN_FAILURES_BEFORE_ISSUE:
                self._raise_issue(
                    issue_id,
                    "token_rejected",
                    ir.IssueSeverity.WARNING,
                    "guardia/vigilar/credencial-invalida",
                    bulb=state.name,
                )
            elif state.token_failures == 0:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _issue_restart_failed(self, state: BulbState) -> None:
        self._raise_issue(
            f"restart_failed_{state.did}",
            "restart_failed",
            ir.IssueSeverity.ERROR,
            "guardia/vigilar/firmware-colgado",
            bulb=state.name,
        )

    def snapshot(self) -> dict[int, BulbState]:
        return {did: replace(state) for did, state in self._states.items()}
