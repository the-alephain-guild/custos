"""A redactor that holds credential values must never print them itself.

The runtime-log redactor is built with the exact secret values it replaces. Any
place that renders the object -- a repr in a log line, an f-string, an assertion
explanation -- would otherwise put those values in the output it exists to keep
them out of.
"""

from __future__ import annotations

import copy
import pickle

import pytest
from structlog.testing import capture_logs

from custos.core.engine_lifecycle import EngineLifecycleQuarantined
from custos.core.runtime_log_fact import RuntimeLogRedactor
from tests.test_engine_lifecycle import _Artifact, _Engine, _Store, _supervisor, _verified

FAKE_KEY = "fake-api-key-0000-1111"
FAKE_SECRET = "fake-api-secret-2222-3333"
FAKE_PASSPHRASE = "fake-okx-passphrase-4444"


def test_the_redactor_does_not_print_the_values_it_holds() -> None:
    redactor = RuntimeLogRedactor((FAKE_KEY, FAKE_SECRET))

    rendered = (repr(redactor), str(redactor), f"{redactor}", f"{redactor!r}")

    for text in rendered:
        assert FAKE_KEY not in text
        assert FAKE_SECRET not in text
        assert text == "RuntimeLogRedactor(<2 known secrets>)"


def test_a_redactor_still_replaces_the_values_it_does_not_print() -> None:
    redactor = RuntimeLogRedactor((FAKE_KEY,))

    assert redactor.message(f"rejected {FAKE_KEY}") == "rejected <redacted>"


async def test_a_failed_start_does_not_log_the_okx_passphrase() -> None:
    engine = _Engine([RuntimeError(f"venue refused login {FAKE_PASSPHRASE}")])

    with capture_logs() as events, pytest.raises(EngineLifecycleQuarantined):
        await _supervisor(_Store(), engine, restart_budget=0).apply(
            delivery_id="delivery-passphrase",
            verified=_verified(),
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={
                "api_key": FAKE_KEY,
                "api_secret": FAKE_SECRET,
                "api_passphrase": FAKE_PASSPHRASE,
            },
            artifact=_Artifact(),
        )

    failures = [event for event in events if event["event"] == "engine_start_attempt_failed"]
    assert len(failures) == 1
    assert FAKE_PASSPHRASE not in failures[0]["error"]
    assert failures[0]["error"] == "venue refused login <redacted>"


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_a_redactor_refuses_to_be_pickled(protocol: int) -> None:
    redactor = RuntimeLogRedactor((FAKE_KEY,))

    with pytest.raises(TypeError, match="cannot be pickled or copied"):
        pickle.dumps(redactor, protocol=protocol)


@pytest.mark.parametrize("duplicate", [copy.copy, copy.deepcopy])
def test_a_redactor_refuses_to_be_copied(duplicate) -> None:
    redactor = RuntimeLogRedactor((FAKE_KEY,))

    with pytest.raises(TypeError, match="cannot be pickled or copied"):
        duplicate(redactor)
