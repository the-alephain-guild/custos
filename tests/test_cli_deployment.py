"""``arx-runner deployment {validate,publish}`` — the offline lane's entry points.

The exact flags asserted here are the ones `philosophers-stone/deploy/custos`
passes from its compose file and Makefile. Renaming or dropping one is a silent
break for the consumer this surface exists to serve, so the invocation itself is
a test rather than a convention.

Transport is faked, but the fake asserts what was actually sent — the URL, the
subject and the bytes — because a mock that only returns success proves the code
ran, not that it did the right thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from custos.cli.subcommands import main
from custos.offline.spec import OfflineDeploymentMessage, compute_strategy_code_hash

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SAMPLE = ROOT / "docs/gateway-contract/v1/samples/offline_deployment_spec_sandbox.json"


@pytest.fixture
def strategy_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "strategy"
    directory.mkdir()
    (directory / "strategy.py").write_text("class S: ...\n", encoding="utf-8")
    return directory


def _spec_file(tmp_path: Path, **overrides: Any) -> Path:
    document = json.loads(SANDBOX_SAMPLE.read_text(encoding="utf-8"))
    document.update(overrides)
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class _RecordingJetStream:
    def __init__(self, ack: object) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._ack = ack

    async def publish(self, subject: str, payload: bytes) -> object:
        self.published.append((subject, payload))
        if isinstance(self._ack, Exception):
            raise self._ack
        return self._ack


class _RecordingConnection:
    def __init__(self, ack: object) -> None:
        self.jetstream_client = _RecordingJetStream(ack)
        self.drained = False

    def jetstream(self) -> _RecordingJetStream:
        return self.jetstream_client

    async def drain(self) -> None:
        self.drained = True


class _RecordingConnect:
    """Stands in for ``nats.connect`` and remembers whether it was reached."""

    def __init__(self, ack: object = object()) -> None:
        self.urls: list[str] = []
        self.connection = _RecordingConnection(ack)

    async def __call__(self, url: str) -> _RecordingConnection:
        self.urls.append(url)
        return self.connection


def _publish_argv(spec_file: Path, strategy_dir: Path, **overrides: str) -> list[str]:
    argv = {
        "--spec-file": str(spec_file),
        "--tenant-id": "local",
        "--strategy-id": "supertrend-sandbox",
        "--nats-url": "nats://nats:4222",
        "--strategy-dir": str(strategy_dir),
    }
    argv.update(overrides)
    return ["deployment", "publish", *[token for pair in argv.items() for token in pair]]


def test_deployment_is_registered_on_the_cli_surface(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["deployment", "--help"])

    assert "validate" in capsys.readouterr().out


def test_the_consumer_validate_invocation_is_accepted(tmp_path: Path, strategy_dir: Path) -> None:
    """`deploy/custos/Makefile` spec-validate passes exactly these flags."""

    spec_file = _spec_file(tmp_path)

    assert (
        main(
            [
                "deployment",
                "validate",
                "--spec-file",
                str(spec_file),
                "--strategy-dir",
                str(strategy_dir),
            ]
        )
        == 0
    )


def test_validate_fills_in_an_absent_digest_from_the_strategy_directory(
    tmp_path: Path, strategy_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The consumer renders a null digest and expects the directory to supply it."""

    spec_file = _spec_file(tmp_path, code_hash=None)

    assert (
        main(
            [
                "deployment",
                "validate",
                "--spec-file",
                str(spec_file),
                "--strategy-dir",
                str(strategy_dir),
            ]
        )
        == 0
    )
    assert compute_strategy_code_hash(strategy_dir)[:12] in capsys.readouterr().out


def test_validate_rejects_a_declared_digest_that_does_not_match(
    tmp_path: Path, strategy_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_file = _spec_file(tmp_path, code_hash="b" * 64)

    assert (
        main(
            [
                "deployment",
                "validate",
                "--spec-file",
                str(spec_file),
                "--strategy-dir",
                str(strategy_dir),
            ]
        )
        == 1
    )
    assert "differs" in capsys.readouterr().err


def test_validate_rejects_a_missing_strategy_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_file = _spec_file(tmp_path)

    assert (
        main(
            [
                "deployment",
                "validate",
                "--spec-file",
                str(spec_file),
                "--strategy-dir",
                str(tmp_path / "absent"),
            ]
        )
        == 1
    )
    assert "strategy directory" in capsys.readouterr().err


def test_validate_rejects_a_live_spec(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec_file = _spec_file(tmp_path, trading_mode="live")

    assert main(["deployment", "validate", "--spec-file", str(spec_file)]) == 1
    assert "live" in capsys.readouterr().err


def test_validate_rejects_a_mode_the_caller_disagrees_with(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_file = _spec_file(tmp_path)

    assert main(["deployment", "validate", "--spec-file", str(spec_file), "--mode", "testnet"]) == 1
    assert "disagree" in capsys.readouterr().err


def test_validate_rejects_an_unreadable_spec_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deployment", "validate", "--spec-file", str(tmp_path / "absent.json")]) == 1
    assert capsys.readouterr().err


def test_validate_rejects_a_malformed_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_file = _spec_file(tmp_path, generation=0)

    assert main(["deployment", "validate", "--spec-file", str(spec_file)]) == 1
    assert "generation" in capsys.readouterr().err


def test_publish_sends_the_spec_to_the_subject_bound_to_its_tenant_and_strategy(
    tmp_path: Path, strategy_dir: Path
) -> None:
    spec_file = _spec_file(tmp_path)
    connect = _RecordingConnect(ack=mock.Mock(seq=1))

    with mock.patch("custos.cli.subcommands.deployment.nats.connect", connect):
        assert main(_publish_argv(spec_file, strategy_dir)) == 0

    assert connect.urls == ["nats://nats:4222"]
    (subject, payload) = connect.connection.jetstream_client.published[0]
    assert subject == "arx.local.deployment_spec.supertrend-sandbox"
    parsed = OfflineDeploymentMessage.parse(payload, expected_tenant_id="local")
    assert parsed.spec.spec_id == "supertrend-sandbox"
    assert parsed.spec.code_hash == compute_strategy_code_hash(strategy_dir)
    assert connect.connection.drained


def test_publish_refuses_live_before_reaching_the_transport(
    tmp_path: Path, strategy_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal that arrives after connecting has already published intent."""

    spec_file = _spec_file(tmp_path, trading_mode="live")
    connect = _RecordingConnect()

    with mock.patch("custos.cli.subcommands.deployment.nats.connect", connect):
        assert main(_publish_argv(spec_file, strategy_dir)) == 1

    assert connect.urls == []
    assert "live" in capsys.readouterr().err


def test_publish_refuses_a_mode_disagreement_before_reaching_the_transport(
    tmp_path: Path, strategy_dir: Path
) -> None:
    spec_file = _spec_file(tmp_path)
    connect = _RecordingConnect()

    with mock.patch("custos.cli.subcommands.deployment.nats.connect", connect):
        assert main(_publish_argv(spec_file, strategy_dir, **{"--mode": "testnet"})) == 1

    assert connect.urls == []


def test_publish_reports_a_transport_failure_without_claiming_success(
    tmp_path: Path, strategy_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_file = _spec_file(tmp_path)
    connect = _RecordingConnect(ack=RuntimeError("jetstream unavailable"))

    with mock.patch("custos.cli.subcommands.deployment.nats.connect", connect):
        assert main(_publish_argv(spec_file, strategy_dir)) == 1

    assert "jetstream unavailable" in capsys.readouterr().err
    assert connect.connection.drained


def test_publish_treats_a_missing_acknowledgement_as_failure(
    tmp_path: Path, strategy_dir: Path
) -> None:
    spec_file = _spec_file(tmp_path)
    connect = _RecordingConnect(ack=None)

    with mock.patch("custos.cli.subcommands.deployment.nats.connect", connect):
        assert main(_publish_argv(spec_file, strategy_dir)) == 1


def test_publish_rejects_an_unsafe_tenant_id(tmp_path: Path, strategy_dir: Path) -> None:
    spec_file = _spec_file(tmp_path)
    connect = _RecordingConnect()

    with mock.patch("custos.cli.subcommands.deployment.nats.connect", connect):
        with pytest.raises(SystemExit):
            main(_publish_argv(spec_file, strategy_dir, **{"--tenant-id": "../escape"}))

    assert connect.urls == []
