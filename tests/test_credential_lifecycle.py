"""Credential lifecycle invariant — the decrypted key never surfaces through a
plain object walk of the NT config the host builds (non-custodial red line 0.1).

Three defence-in-depth invariants guard the in-process credential; this file
covers invariant #2. The other two are already covered elsewhere and are cross
referenced rather than duplicated:

* invariant #1 (repr) — test_nt_trading_node_host.py::test_deploy_does_not_retain_credential
* invariant #3 (structlog redaction) — test_nt_trading_node_host.py::test_exception_log_redacts_credential_material

Invariant #2: the raw credential lives only inside the NautilusTrader client
configs; a recursive ``__dict__`` walk of the objects the host assembles must not
reach it. That guarantees naive introspection or serialisation of a config
(``vars(...)`` → json) can never leak a key, even though an authenticated config
legitimately carries it in memory to sign requests — the red line is the I/O
boundary (log / publish / network), not in-memory config state.

The invariant is asserted on the client configs rather than on a live node:
constructing a native node reinitialises NautilusTrader's global Rust logging
subsystem, which aborts (SIGABRT) on a second construction inside the shared test
process.

2.0 strengthens this. Its config objects are rust pyclasses that expose no
``api_key`` attribute at all and redact their repr, so the credential is not
readable back by any route, not merely absent from ``__dict__``. Both are
asserted: the walk, which is the invariant this file has always held, and the
absence of the attribute, which is new and is what makes the walk hold trivially.

**Scope**: this walks the config surface reachable via ``__dict__`` + container
expansion (depth 5). It does *not* walk a post-construction node object graph --
that fuller coverage is blocked by the SIGABRT above and deferred to a
subprocess-isolated follow-up (Plan 05 candidate, see
DEV-03-CREDENTIAL-TEST-NO-NATIVE-NODE). The configs are the objects custos
assembles that carry the key, so the credential-carrying surface is covered; the
gap is the native node wrapper's own attributes, which custos does not populate
with the raw credential.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")

from custos.engines.nautilus import venue_binance as venue  # noqa: E402

_SENTINEL_KEY = "SENSITIVE_KEY_XYZ"
_SENTINEL_SECRET = "SENSITIVE_SECRET_ABC"


def _walk_dict(obj: object, depth: int = 5) -> list[str]:
    """Collect every str leaf reachable from ``obj`` via ``__dict__`` / list /
    dict / tuple / set expansion, bounded to ``depth`` nesting levels.

    msgspec Structs (NT config objects) use ``__slots__`` and expose no
    ``__dict__``, so their fields are never descended into — which is exactly the
    invariant under test.
    """
    found: list[str] = []
    seen: set[int] = set()

    def _visit(node: object, remaining: int) -> None:
        if remaining < 0 or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, str):
            found.append(node)
            return
        if isinstance(node, (bytes, bytearray)):
            return
        if isinstance(node, dict):
            for key, value in node.items():
                _visit(key, remaining - 1)
                _visit(value, remaining - 1)
            return
        if isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                _visit(item, remaining - 1)
            return
        attrs = getattr(node, "__dict__", None)
        if attrs:
            _visit(attrs, remaining - 1)

    _visit(obj, depth)
    return found


def _spec(spec_id: str = "cred-spec") -> dict:
    return {
        "spec_id": spec_id,
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }


def _credential() -> dict:
    return {
        "api_key": _SENTINEL_KEY,
        "api_secret": _SENTINEL_SECRET,
        "permission_scope": "trade_no_withdraw",
    }


def test_sandbox_configs_never_receive_the_credential() -> None:
    """Sandbox market data is public and its execution is local, so nothing
    authenticated is assembled at all -- the credential does not enter the graph."""
    spec = _spec()
    credential = _credential()

    data_cfg = venue.build_data_client_config(
        spec, credential, venue.data_environment_for_mode("sandbox")
    )
    exec_cfg = venue.build_exec_client_config_sandbox(spec, credential, ["10_000 USDT"])

    for cfg in (data_cfg, exec_cfg):
        leaves = _walk_dict(cfg, depth=5)
        assert _SENTINEL_KEY not in leaves
        assert _SENTINEL_SECRET not in leaves


def test_an_authenticated_config_does_not_surrender_the_credential() -> None:
    """The case with something to leak: testnet signs, so these configs are handed
    the real key. It must still be unreachable by a plain object walk, and in 2.0
    it is not readable back by any route -- there is no attribute for it and the
    repr is redacted."""
    spec = dict(_spec(), trading_mode="testnet")
    credential = _credential()

    data_cfg = venue.build_data_client_config(
        spec, credential, venue.data_environment_for_mode("testnet")
    )
    exec_cfg = venue.build_exec_client_config_testnet(spec, credential)

    for cfg in (data_cfg, exec_cfg):
        leaves = _walk_dict(cfg, depth=5)
        assert _SENTINEL_KEY not in leaves
        assert _SENTINEL_SECRET not in leaves
        assert not hasattr(cfg, "api_key")
        assert not hasattr(cfg, "api_secret")
        assert _SENTINEL_KEY not in repr(cfg)
        assert _SENTINEL_SECRET not in repr(cfg)
