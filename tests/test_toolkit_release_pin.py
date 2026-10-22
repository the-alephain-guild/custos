"""A runner release is refused while the toolkit differs from the authorized rc."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-toolkit-release-pin.py"
RECEIPT = "docs/authority/receipts/custos-toolkit-rcX-authority-v1.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_toolkit_release_pin", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pin = _load()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "LC_ALL": "C",
        },
    ).stdout.strip()


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repository(tmp_path: Path, *, receipt_digest_ok: bool = True) -> tuple[Path, str]:
    """A repository whose first commit is the rc source and registers that rc."""

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "packages/custos-strategy-toolkit/src/base.py", "BASE = 1\n")
    _write(root, "packages/custos-strategy-toolkit-nautilus/src/adapter.py", "ADAPTER = 1\n")
    _write(root, RECEIPT, '{"candidate_version": "0.1.0rcX"}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "rc source")
    source = _git(root, "rev-parse", "HEAD")
    digest = hashlib.sha256((root / RECEIPT).read_bytes()).hexdigest()
    authority = {
        "toolkit_rc_release": {
            "receipt_status": "READY_TOOLKIT_RC",
            "receipt_present": True,
            "receipt_path": RECEIPT,
            "receipt_sha256": digest if receipt_digest_ok else "0" * 64,
            "candidate_version": "0.1.0rcX",
            "source_commit": source,
            "manifest_digest": "sha256:" + "a" * 64,
        }
    }
    _write(root, "docs/authority/ecosystem-authority.json", json.dumps(authority))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "register rc")
    return root, source


def test_a_toolkit_equal_to_the_rc_source_is_released(tmp_path: Path) -> None:
    root, source = _repository(tmp_path)

    result = pin.check(root)

    assert result["toolkit_matches_rc"] is True
    assert result["differing_files"] == []
    assert result["source_commit"] == source
    assert pin.main(["--root", str(root), "--release"]) == 0


@pytest.mark.parametrize(
    "changed",
    [
        "packages/custos-strategy-toolkit/src/base.py",
        "packages/custos-strategy-toolkit-nautilus/src/adapter.py",
    ],
)
def test_any_toolkit_change_after_the_rc_refuses_the_release(tmp_path: Path, changed: str) -> None:
    root, _ = _repository(tmp_path)
    _write(root, changed, "CHANGED = 2\n")
    _git(root, "commit", "-q", "-am", "toolkit change")

    result = pin.check(root)

    assert result["toolkit_matches_rc"] is False
    assert result["differing_files"] == [changed]
    assert pin.main(["--root", str(root), "--release"]) == 1
    # Without --release the same state is only reported.
    assert pin.main(["--root", str(root)]) == 0


def test_a_change_outside_the_toolkit_does_not_need_an_rc(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    _write(root, "src/custos/runner.py", "RUNNER = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "runner change")

    assert pin.check(root)["toolkit_matches_rc"] is True


def test_a_receipt_that_differs_from_its_registered_digest_is_refused(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path, receipt_digest_ok=False)

    with pytest.raises(pin.ReleasePinError, match="differs from its registered digest"):
        pin.check(root)
    assert pin.main(["--root", str(root), "--release"]) == 2


def test_an_rc_that_is_not_ready_is_refused(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    path = root / "docs/authority/ecosystem-authority.json"
    document = json.loads(path.read_text())
    document["toolkit_rc_release"]["receipt_status"] = "PENDING"
    path.write_text(json.dumps(document))

    with pytest.raises(pin.ReleasePinError, match="not READY_TOOLKIT_RC"):
        pin.check(root)
