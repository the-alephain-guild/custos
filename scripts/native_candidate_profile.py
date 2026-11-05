"""Render static engine definitions for an explicit distribution build profile."""

from __future__ import annotations

import json

_ENGINES = {
    "released-sodex3-v1": "2.0.0rc5+sodex.3",
    "native-capture-darwin7-v1": "2.0.0rc5+papertrade.7",
}
_DEFINITIONS = (
    "packages/custos-strategy-toolkit/src/custos_toolkit/contracts/engine_profile.py",
    "packages/custos-strategy-publisher/src/custos_strategy_publisher/engine_profile.py",
)


def render_engine_definitions(profile_id: str) -> dict[str, bytes]:
    """Return build inputs without changing the source checkout or process imports."""
    try:
        engine = _ENGINES[profile_id]
    except (KeyError, TypeError):
        raise ValueError("unknown engine build profile") from None
    content = (
        '"""Static engine contract selected when this distribution is built."""\n\n'
        "from typing import Final, Literal\n\n"
        f"ENGINE_PROFILE_ID: Final = {json.dumps(profile_id)}\n"
        f"ENGINE_VERSION: Final = {json.dumps(engine)}\n"
        f"EngineVersion = Literal[{json.dumps(engine)}]\n"
    ).encode()
    return dict.fromkeys(_DEFINITIONS, content)
