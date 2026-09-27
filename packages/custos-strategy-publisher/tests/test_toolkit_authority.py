"""The toolkit authority comes from Custos's own receipt, not from a producer's copy.

The values checked here are the ones the first producer recorded in its own lock
for the same release candidate, so reading Custos's receipt directly binds the
same toolkit that producer's releases were built against.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from custos_strategy_publisher.toolkit_authority import (
    AUTHORITY_RECEIPT_PATH,
    ToolkitAuthorityError,
    load_toolkit_authority,
)

CUSTOS_ROOT = Path(__file__).resolve().parents[3]
RC8_AUTHORITY_COMMIT = "4c5112da29d19ade016e8630cf576678e2e484d2"


def _load(root: Path = CUSTOS_ROOT, **overrides: str):
    arguments = {"candidate_version": "0.1.0rc8", "authority_commit": RC8_AUTHORITY_COMMIT}
    arguments.update(overrides)
    return load_toolkit_authority(root, **arguments)


def test_rc8_binds_the_toolkit_the_first_producer_locked() -> None:
    authority = _load()

    assert authority.candidate_version == "0.1.0rc8"
    assert authority.registry == "ghcr.io"
    assert authority.repository == "the-alephain-guild/custos-strategy-toolkit"
    assert authority.manifest_digest == (
        "sha256:94e8a3ecfca41f610061217b811cedf3d3e0a82e0e70fb3d3a53dd11124c2be5"
    )
    assert authority.manifest_size_bytes == 7593
    assert authority.source_commit == "6b41727c26d51f686ada2186fad35373854a35d1"
    assert authority.authority_commit == RC8_AUTHORITY_COMMIT
    assert authority.authority_receipt.sha256 == (
        "d22dbe5d16a59edac1f2996cf5836039e4648f407a38d4cb3acee0e96c324768"
    )
    assert authority.base_contracts_wheel.title == (
        "custos_strategy_toolkit-0.1.0rc8-py3-none-any.whl"
    )


def test_a_version_custos_has_no_receipt_for_is_refused() -> None:
    with pytest.raises(ToolkitAuthorityError, match="no authority receipt for 0.1.0rc99"):
        _load(candidate_version="0.1.0rc99")


def test_a_version_that_is_not_a_release_candidate_is_refused() -> None:
    with pytest.raises(ToolkitAuthorityError, match="not a release candidate"):
        _load(candidate_version="0.1.0")


def test_an_authority_commit_that_is_not_a_commit_is_refused() -> None:
    with pytest.raises(ToolkitAuthorityError, match="authority commit"):
        _load(authority_commit="main")


def test_a_receipt_that_differs_from_its_sidecar_is_refused(tmp_path: Path) -> None:
    receipt = AUTHORITY_RECEIPT_PATH.format(release="rc8")
    target = tmp_path / receipt
    target.parent.mkdir(parents=True)
    shutil.copy(CUSTOS_ROOT / f"{receipt}.sha256", tmp_path / f"{receipt}.sha256")
    target.write_bytes((CUSTOS_ROOT / receipt).read_bytes() + b"\n")

    with pytest.raises(ToolkitAuthorityError, match="differ from its sha256 sidecar"):
        _load(tmp_path)
