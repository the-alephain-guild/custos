"""The runtime toolkit identity, the build identity and the judgement between them."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import structlog

from custos.core.toolkit_identity import (
    BuildToolkitIdentity,
    Mismatch,
    RuntimeToolkitIdentity,
    ToolkitConsistencyState,
    build_identity_from_activation_root,
    build_identity_from_release_bom,
    build_identity_from_wheel_metadata,
    judge,
    observe_runtime_toolkit_identity,
)

RC = "0.1.0rc10"
MANIFEST = "sha256:" + "9" * 64
WHEEL = "a" * 64
ENGINE = "2.0.0rc5+sodex.3"


def _installed(versions: dict[str, str]):
    def version(name: str) -> str:
        if name not in versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    return version


INSTALLED_RC = _installed(
    {
        "custos-strategy-toolkit": RC,
        "custos-strategy-toolkit-nautilus": RC,
        "nautilus-trader": ENGINE,
    }
)


def _identity_file(directory: Path, **overrides: object) -> Path:
    document = {
        "schema_version": 1,
        "candidate_version": RC,
        "manifest_digest": MANIFEST,
        "receipt_sha256": "b" * 64,
        "source_commit": "c" * 40,
        "base_contracts_wheel_sha256": "d" * 64,
        "nautilus_wheel_sha256": WHEEL,
        **overrides,
    }
    path = directory / "toolkit-identity.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _runtime(**overrides: object) -> RuntimeToolkitIdentity:
    values: dict[str, object] = {
        "trusted": True,
        "candidate_version": RC,
        "manifest_digest": MANIFEST,
        "nautilus_wheel_sha256": WHEEL,
        "engine_version": ENGINE,
        **overrides,
    }
    return RuntimeToolkitIdentity(**values)  # type: ignore[arg-type]


def _bom(**overrides: object) -> dict[str, object]:
    return {
        "toolkit_coordinate": f"ghcr.io/example/custos-strategy-toolkit@{MANIFEST}",
        "toolkit_wheel_sha256": WHEEL,
        "engine_version": ENGINE,
        **overrides,
    }


# The runtime identity.


def test_an_identity_that_agrees_with_the_installed_toolkit_is_the_runtime_rc(
    tmp_path: Path,
) -> None:
    runtime = observe_runtime_toolkit_identity(_identity_file(tmp_path), INSTALLED_RC)

    assert runtime == _runtime()


def test_no_identity_file_is_a_source_build(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as logs:
        runtime = observe_runtime_toolkit_identity(tmp_path / "absent.json", INSTALLED_RC)

    assert runtime.trusted is False
    assert runtime.candidate_version is None
    assert runtime.engine_version == ENGINE
    assert logs == []


def test_an_identity_the_installed_versions_contradict_is_untrusted_and_logged(
    tmp_path: Path,
) -> None:
    installed = _installed(
        {
            "custos-strategy-toolkit": "0.1.0",
            "custos-strategy-toolkit-nautilus": RC,
            "nautilus-trader": ENGINE,
        }
    )
    with structlog.testing.capture_logs() as logs:
        runtime = observe_runtime_toolkit_identity(_identity_file(tmp_path), installed)

    assert runtime.trusted is False
    assert runtime.candidate_version is None
    assert runtime.manifest_digest is None
    assert [(e["event"], e["log_level"]) for e in logs] == [
        ("runtime_toolkit_identity_untrusted", "error")
    ]


def test_an_identity_file_that_is_a_symbolic_link_is_untrusted_and_logged(
    tmp_path: Path,
) -> None:
    target = _identity_file(tmp_path)
    link = tmp_path / "linked-identity.json"
    link.symlink_to(target)
    with structlog.testing.capture_logs() as logs:
        runtime = observe_runtime_toolkit_identity(link, INSTALLED_RC)

    assert runtime.trusted is False
    assert [e["event"] for e in logs] == ["runtime_toolkit_identity_untrusted"]


# The judgement.


def test_a_release_built_on_the_running_toolkit_and_engine_is_consistent() -> None:
    result = judge(build_identity_from_release_bom(_bom()), _runtime())

    assert result.state is ToolkitConsistencyState.CONSISTENT
    assert result.mismatches == ()
    assert result.refusal_reason() is None


def test_the_same_manifest_with_another_adapter_wheel_differs() -> None:
    result = judge(build_identity_from_release_bom(_bom(toolkit_wheel_sha256="e" * 64)), _runtime())

    assert result.state is ToolkitConsistencyState.DIFFERS
    assert result.mismatches == (Mismatch.TOOLKIT_WHEEL,)
    assert result.refusal_reason() == "runtime_toolkit_differs_from_build"


def test_the_same_toolkit_on_another_engine_differs_by_the_engine_only() -> None:
    result = judge(
        build_identity_from_release_bom(_bom(engine_version="2.0.0rc5+sodex.2")), _runtime()
    )

    assert result.state is ToolkitConsistencyState.DIFFERS
    assert result.mismatches == (Mismatch.ENGINE,)
    assert result.refusal_reason() == "runtime_engine_differs_from_build"


def test_another_manifest_differs_by_the_manifest() -> None:
    result = judge(
        build_identity_from_release_bom(
            _bom(toolkit_coordinate="ghcr.io/example/custos-strategy-toolkit@sha256:" + "1" * 64)
        ),
        _runtime(),
    )

    assert result.mismatches == (Mismatch.TOOLKIT_MANIFEST,)


@pytest.mark.parametrize(
    "missing", ["toolkit_coordinate", "toolkit_wheel_sha256", "engine_version"]
)
def test_a_bom_without_a_toolkit_field_is_unreadable(missing: str) -> None:
    bom = _bom()
    del bom[missing]

    result = judge(build_identity_from_release_bom(bom), _runtime())

    assert result.state is ToolkitConsistencyState.BUILD_UNREADABLE
    assert result.refusal_reason() == "build_toolkit_unreadable"


def test_a_source_build_runtime_is_neither_consistent_nor_given_a_version() -> None:
    result = judge(build_identity_from_release_bom(_bom()), _runtime(trusted=False))

    assert result.state is ToolkitConsistencyState.RUNTIME_SOURCE_BUILD
    assert result.runtime_candidate_version is None
    assert result.refusal_reason() == "runtime_toolkit_source_build"


def _metadata(*requires: str) -> str:
    lines = ["Metadata-Version: 2.1", "Name: example-strategy", "Version: 2.1.1"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    return "\n".join(lines) + "\n\n"


def test_a_development_snapshot_pinned_to_the_running_rc_and_engine_is_consistent() -> None:
    build = build_identity_from_wheel_metadata(
        _metadata(f"custos-strategy-toolkit=={RC}", f"nautilus-trader=={ENGINE}")
    )

    assert judge(build, _runtime()).state is ToolkitConsistencyState.CONSISTENT


def test_a_development_snapshot_pinned_to_another_toolkit_and_engine_differs() -> None:
    build = build_identity_from_wheel_metadata(
        _metadata(
            "custos_strategy_toolkit (==0.1.0)",
            "nautilus-trader[sodex]==2.0.0rc5+sodex.1 ; python_version >= '3.12'",
        )
    )
    result = judge(build, _runtime())

    assert result.state is ToolkitConsistencyState.DIFFERS
    assert result.mismatches == (Mismatch.TOOLKIT_VERSION, Mismatch.ENGINE)


@pytest.mark.parametrize(
    "requires",
    [
        ("nautilus-trader==2.0.0rc5+sodex.3",),
        (f"custos-strategy-toolkit=={RC}",),
        (f"custos-strategy-toolkit>={RC}", "nautilus-trader==2.0.0rc5+sodex.3"),
    ],
)
def test_a_development_snapshot_without_both_exact_pins_is_unreadable(
    requires: tuple[str, ...],
) -> None:
    result = judge(build_identity_from_wheel_metadata(_metadata(*requires)), _runtime())

    assert result.state is ToolkitConsistencyState.BUILD_UNREADABLE


def test_a_development_build_identity_is_read_from_the_activated_wheel(tmp_path: Path) -> None:
    dist_info = tmp_path / "example_strategy-2.1.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        _metadata(f"custos-strategy-toolkit=={RC}", "nautilus-trader==2.0.0rc5+sodex.1"),
        encoding="utf-8",
    )

    build = build_identity_from_activation_root(tmp_path)

    assert build == BuildToolkitIdentity(
        "development", toolkit_version=RC, engine_version="2.0.0rc5+sodex.1"
    )


def test_the_engine_is_compared_for_a_source_build_too() -> None:
    build = build_identity_from_wheel_metadata(
        _metadata(f"custos-strategy-toolkit=={RC}", "nautilus-trader==2.0.0rc5+sodex.1")
    )
    result = judge(build, _runtime(trusted=False, candidate_version=None))

    assert result.state is ToolkitConsistencyState.RUNTIME_SOURCE_BUILD
    assert result.mismatches == (Mismatch.ENGINE,)


# The interface the decision-mode and strategy-log gates read.


def test_a_consistent_live_judgement_names_the_runtime_candidate(tmp_path: Path) -> None:
    runtime = observe_runtime_toolkit_identity(_identity_file(tmp_path), INSTALLED_RC)

    result = judge(build_identity_from_release_bom(_bom()), runtime)

    assert result.consistent is True
    assert result.runtime_candidate_version == RC


def test_an_untrusted_runtime_gives_no_candidate_version_rather_than_a_default(
    tmp_path: Path,
) -> None:
    installed = _installed(
        {
            "custos-strategy-toolkit": "0.1.0",
            "custos-strategy-toolkit-nautilus": "0.1.0",
            "nautilus-trader": ENGINE,
        }
    )
    runtime = observe_runtime_toolkit_identity(_identity_file(tmp_path), installed)

    result = judge(build_identity_from_release_bom(_bom()), runtime)

    assert result.consistent is False
    assert result.runtime_candidate_version is None


# What makes a runtime identity untrusted, case by case.


@pytest.mark.parametrize(
    ("installed", "overrides"),
    [
        (
            {
                "custos-strategy-toolkit": RC,
                "custos-strategy-toolkit-nautilus": "0.1.0",
                "nautilus-trader": ENGINE,
            },
            {},
        ),
        (None, {"schema_version": 2}),
        (None, {"schema_version": None}),
    ],
    ids=["adapter-version-differs", "schema-2", "schema-missing"],
)
def test_an_identity_failing_one_cross_check_is_untrusted_and_logged(
    tmp_path: Path, installed: dict[str, str] | None, overrides: dict[str, object]
) -> None:
    path = _identity_file(tmp_path, **overrides)
    if overrides.get("schema_version", 1) is None:
        document = json.loads(path.read_text())
        del document["schema_version"]
        path.write_text(json.dumps(document))
    with structlog.testing.capture_logs() as logs:
        runtime = observe_runtime_toolkit_identity(
            path, _installed(installed) if installed else INSTALLED_RC
        )

    assert runtime.trusted is False
    assert [e["event"] for e in logs] == ["runtime_toolkit_identity_untrusted"]


def test_an_identity_path_that_is_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    """Opening a FIFO with no writer would wait forever; the check must not open it."""

    fifo = tmp_path / "toolkit-identity.json"
    os.mkfifo(fifo)
    code = (
        "from pathlib import Path; import sys; "
        "from custos.core.toolkit_identity import observe_runtime_toolkit_identity as o; "
        "print(o(Path(sys.argv[1]), report=lambda e, f: print(e)).trusted)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code, str(fifo)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["runtime_toolkit_identity_untrusted", "False"]


def test_an_engine_whose_metadata_cannot_be_read_is_no_engine_and_is_reported(
    tmp_path: Path,
) -> None:
    def broken(name: str) -> str:
        if name == "nautilus-trader":
            raise ValueError("corrupt METADATA")
        return RC

    reported: list[str] = []
    runtime = observe_runtime_toolkit_identity(
        _identity_file(tmp_path), broken, report=lambda event, _fields: reported.append(event)
    )
    result = judge(build_identity_from_release_bom(_bom()), runtime)

    assert runtime.engine_version is None
    assert reported == ["runtime_engine_version_unreadable"]
    assert result.refusal_reason() == "runtime_engine_differs_from_build"


def test_wheel_metadata_that_is_not_utf8_is_unreadable(tmp_path: Path) -> None:
    dist_info = tmp_path / "example_strategy-2.1.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_bytes(b"Metadata-Version: 2.1\nName: \xff\xfe\n")

    build = build_identity_from_activation_root(tmp_path)

    assert build.unreadable is not None
    assert judge(build, _runtime()).state is ToolkitConsistencyState.BUILD_UNREADABLE


# Which pins of a development snapshot apply here.


def test_only_the_pin_whose_marker_holds_here_is_read() -> None:
    build = build_identity_from_wheel_metadata(
        _metadata(
            f"custos-strategy-toolkit=={RC}",
            'nautilus-trader==2.0.0rc5+sodex.3 ; python_version < "3.0"',
            'nautilus-trader==2.0.0rc5+sodex.1 ; python_version >= "3.0"',
            'nautilus-trader==9.9.9 ; extra == "research"',
        )
    )

    assert build.engine_version == "2.0.0rc5+sodex.1"
    assert judge(build, _runtime()).mismatches == (Mismatch.ENGINE,)


@pytest.mark.parametrize(
    "requires",
    [
        (
            f"custos-strategy-toolkit=={RC}",
            "nautilus-trader==2.0.0rc5+sodex.3",
            "nautilus-trader==2.0.0rc5+sodex.1",
        ),
        (
            f"custos-strategy-toolkit=={RC}",
            "custos-strategy-toolkit-nautilus==0.1.0",
            "nautilus-trader==2.0.0rc5+sodex.3",
        ),
        (
            f"custos-strategy-toolkit=={RC}",
            "nautilus-trader @ https://example.invalid/nautilus_trader-2.0.0-py3-none-any.whl",
        ),
    ],
    ids=["two-engine-pins-apply", "base-and-adapter-differ", "engine-by-url"],
)
def test_pins_that_do_not_name_one_version_are_unreadable(requires: tuple[str, ...]) -> None:
    build = build_identity_from_wheel_metadata(_metadata(*requires))

    assert judge(build, _runtime()).state is ToolkitConsistencyState.BUILD_UNREADABLE


def test_base_and_adapter_pinned_to_the_same_version_read_as_that_version() -> None:
    build = build_identity_from_wheel_metadata(
        _metadata(
            f"custos-strategy-toolkit=={RC}",
            f"custos-strategy-toolkit-nautilus=={RC}",
            f"nautilus-trader=={ENGINE}",
        )
    )

    assert judge(build, _runtime()).state is ToolkitConsistencyState.CONSISTENT


def test_an_identity_in_a_directory_without_permission_is_untrusted_and_reported(
    tmp_path: Path,
) -> None:
    """Examining the path raises PermissionError there; that is an unreadable identity."""

    locked = tmp_path / "locked"
    locked.mkdir()
    path = _identity_file(locked)
    reported: list[str] = []
    locked.chmod(0)
    try:
        runtime = observe_runtime_toolkit_identity(
            path, INSTALLED_RC, report=lambda event, _fields: reported.append(event)
        )
    finally:
        locked.chmod(0o755)

    assert runtime.trusted is False
    assert runtime.engine_version == ENGINE
    assert reported == ["runtime_toolkit_identity_untrusted"]


def test_a_pin_whose_marker_cannot_be_evaluated_is_unreadable() -> None:
    build = build_identity_from_wheel_metadata(
        _metadata(
            f"custos-strategy-toolkit=={RC}",
            'nautilus-trader==2.0.0rc5+sodex.3 ; os_name ~= "posix"',
        )
    )

    assert build.unreadable is not None and "unevaluable marker" in build.unreadable
    assert judge(build, _runtime()).state is ToolkitConsistencyState.BUILD_UNREADABLE
