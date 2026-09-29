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
RC9_AUTHORITY_COMMIT = "a3905828c0ffb17b460d10214a550b60d45f7476"


def _load(root: Path = CUSTOS_ROOT, **overrides: str):
    arguments = {"candidate_version": "0.1.0rc9", "authority_commit": RC9_AUTHORITY_COMMIT}
    arguments.update(overrides)
    return load_toolkit_authority(root, **arguments)


def test_rc9_binds_the_toolkit_the_producer_locks() -> None:
    authority = _load()

    assert authority.candidate_version == "0.1.0rc9"
    assert authority.registry == "ghcr.io"
    assert authority.repository == "the-alephain-guild/custos-strategy-toolkit"
    assert authority.manifest_digest == (
        "sha256:04189c67a255a4e186bd4200a75bdf813d2e94b93de66a5774528e3072c9cee7"
    )
    assert authority.manifest_size_bytes == 7592
    assert authority.source_commit == "8ce29584aa0d7b72eea6bb8cfd5031e201c2970e"
    assert authority.authority_commit == RC9_AUTHORITY_COMMIT
    assert authority.authority_receipt.sha256 == (
        "ed9d0597a181b9073f6ff4b6996ef2f59fb4fd9c74c72704272c968d6d0f20c6"
    )
    assert authority.base_contracts_wheel.title == (
        "custos_strategy_toolkit-0.1.0rc9-py3-none-any.whl"
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
    receipt = AUTHORITY_RECEIPT_PATH.format(release="rc9")
    target = tmp_path / receipt
    target.parent.mkdir(parents=True)
    shutil.copy(CUSTOS_ROOT / f"{receipt}.sha256", tmp_path / f"{receipt}.sha256")
    target.write_bytes((CUSTOS_ROOT / receipt).read_bytes() + b"\n")

    with pytest.raises(ToolkitAuthorityError, match="differ from its sha256 sidecar"):
        _load(tmp_path)
