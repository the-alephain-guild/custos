"""The offline lane's single refusal point for live mode.

Every entry point on this lane passes through here before it parses a spec, opens
a connection or writes anything. Two claims are checked independently — the mode
the spec declares and the mode the caller asked for — because either one can be
the wrong one, and agreeing on the wrong mode is not agreement.
"""

from __future__ import annotations

import json
from typing import Final

from custos.contracts import TradingMode
from custos.core.log import get_logger

_log = get_logger("custos.offline.mode_guard")

PERMITTED_MODES: Final = frozenset({TradingMode.SANDBOX, TradingMode.TESTNET})

_SPEC_SOURCE: Final = "deployment spec"


class OfflineModeRefused(Exception):
    """The offline lane refused a mode it may not run."""


def refuse_live(mode: object, *, source: str) -> TradingMode:
    """Admit a permitted offline mode, or refuse and say where it came from."""

    if not isinstance(mode, str) or not mode.strip():
        _log.warning("offline_mode_refused", reason="absent", source=source)
        raise OfflineModeRefused(f"the offline lane requires {source} to declare a trading mode")
    try:
        candidate = TradingMode(mode)
    except ValueError:
        _log.warning("offline_mode_refused", reason="unknown", source=source, mode=mode)
        raise OfflineModeRefused(
            f"{source} carries an unknown trading mode {mode!r}; "
            f"the offline lane runs {_permitted_description()} only"
        ) from None
    if candidate not in PERMITTED_MODES:
        _log.warning("offline_mode_refused", reason="not_permitted", source=source, mode=mode)
        raise OfflineModeRefused(
            f"{source} carries trading mode {candidate.value!r}; the offline lane runs "
            f"{_permitted_description()} only, and live requires the signed lane"
        )
    return candidate


def admit_offline_spec(raw: bytes | str, *, command_mode: object = None) -> TradingMode:
    """Refuse on either mode claim before the spec is parsed as a contract.

    Returns the single agreed mode. Raises :class:`OfflineModeRefused` if either
    claim is absent, unknown, live, or disagrees with the other.

    ``command_mode=None`` means the caller stated no mode — the spec's own claim
    then stands alone and must still be non-live. That is not the same as an empty
    string, which is a caller stating a mode it cannot name and is refused.
    """

    declared = refuse_live(_declared_mode(raw), source=_SPEC_SOURCE)
    if command_mode is None:
        return declared
    requested = refuse_live(command_mode, source="command line")
    if declared is not requested:
        _log.warning(
            "offline_mode_refused",
            reason="disagreement",
            declared=declared.value,
            requested=requested.value,
        )
        raise OfflineModeRefused(
            f"the deployment spec declares {declared.value!r} but the command line asked for "
            f"{requested.value!r}; the offline lane refuses when the two disagree"
        )
    return declared


def _declared_mode(raw: bytes | str) -> object:
    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        _log.warning("offline_mode_refused", reason="unreadable", source=_SPEC_SOURCE)
        raise OfflineModeRefused(
            "the deployment spec is unreadable, so its trading mode cannot be established"
        ) from exc
    if not isinstance(document, dict):
        raise OfflineModeRefused("the deployment spec must declare a trading mode at its root")
    return document.get("trading_mode")


def _permitted_description() -> str:
    return " and ".join(sorted(mode.value for mode in PERMITTED_MODES))
