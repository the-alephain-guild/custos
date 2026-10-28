"""A local source broker fixture must keep distinct, bounded identities."""

from __future__ import annotations

import base64
import json
import stat
from datetime import datetime
from pathlib import Path

import pytest
from provision_external_source_broker import prepare_environment, start_environment


def test_prepare_two_distinct_subscribers_and_private_material(tmp_path: Path) -> None:
    output = tmp_path / "broker"
    receipt = prepare_environment(output, synthetic=True, port=44223)
    assert receipt["evidenceGrade"] == "synthetic_local_only"
    assert len(receipt["subscribers"]) == 2
    assert receipt["subscribers"][0]["durableName"] != receipt["subscribers"][1]["durableName"]
    assert (
        receipt["subscribers"][0]["credentialsSha256"]
        != receipt["subscribers"][1]["credentialsSha256"]
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name in (
        "admin.creds",
        "publisher.creds",
        "subscriber-0.creds",
        "subscriber-1.creds",
        "source-token",
        "server.key",
    ):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
    assert "source-token" not in (output / "readiness.json").read_text()
    assert "-----BEGIN USER NKEY SEED-----" not in (output / "readiness.json").read_text()
    assert json.loads((output / "readiness.json").read_text()) == receipt
    publisher_jwt = (output / "publisher.creds").read_text().splitlines()[1]
    claims = json.loads(base64.urlsafe_b64decode(publisher_jwt.split(".")[1] + "=="))
    assert claims["exp"] == int(datetime.fromisoformat(receipt["jwtExpiresAt"]).timestamp())


def test_prepare_refuses_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "broker"
    output.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        prepare_environment(output, synthetic=True, port=44223)


def test_exact_manifest_requires_independent_signed_scope_claims(tmp_path: Path) -> None:
    synthetic = tmp_path / "synthetic"
    prepare_environment(synthetic, synthetic=True, port=44223)
    manifest = json.loads((synthetic / "manifest.json").read_text())
    manifest["subscribers"][1]["runnerId"] = manifest["subscribers"][0]["runnerId"]
    with pytest.raises(ValueError, match="distinct"):
        prepare_environment(tmp_path / "duplicate", manifest=manifest, port=44224)
    manifest["subscribers"][1]["runnerId"] = json.loads((synthetic / "manifest.json").read_text())[
        "subscribers"
    ][1]["runnerId"]
    exact = prepare_environment(tmp_path / "exact", manifest=manifest, port=44224)
    assert exact["evidenceGrade"] == "unverified_local_broker"


@pytest.mark.asyncio
async def test_start_refuses_tampered_config_before_docker(tmp_path: Path) -> None:
    output = tmp_path / "broker"
    prepare_environment(output, synthetic=True, port=44223)
    (output / "nats.conf").write_text((output / "nats.conf").read_text() + "\n")
    with pytest.raises(ValueError, match="differs"):
        await start_environment(output)


@pytest.mark.asyncio
async def test_start_refuses_changed_publisher_scope_before_docker(tmp_path: Path) -> None:
    output = tmp_path / "broker"
    prepare_environment(output, synthetic=True, port=44223)
    authorization = output / "publisher-authorization.json"
    document = json.loads(authorization.read_text())
    document["sourceId"] = "another-source"
    authorization.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="publisher authorization differs"):
        await start_environment(output)
