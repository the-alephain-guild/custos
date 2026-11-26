from __future__ import annotations

import io
import stat
import zipfile

import pytest

from custos_strategy_publisher.wheel_archive import (
    WheelArchiveError,
    build_wheel,
    inspect_wheel,
)


def _wheel(version: str = "1.0.0") -> bytes:
    return build_wheel(
        {
            "team_strategy/__init__.py": b"",
            "team_strategy/runtime.py": b"class RuntimeAdapterV1:\n    pass\n",
            "team_strategy/py.typed": b"",
        },
        distribution="team-strategy",
        version=version,
        requires_python=">=3.12,<3.13",
        dependencies=("custos-strategy-toolkit==0.1.0rc5",),
        entry_points={
            "alephain.strategy_runtime.v1": {
                "team-strategy": "team_strategy.runtime:RuntimeAdapterV1"
            }
        },
    )


def test_builds_byte_identical_closed_wheel() -> None:
    first = _wheel()
    second = _wheel()

    assert first == second
    verified = inspect_wheel(
        first,
        distribution="team-strategy",
        version="1.0.0",
        requires_python=">=3.12,<3.13",
        required_modules=("team_strategy",),
    )
    assert verified.content("team_strategy/py.typed") == b""
    assert any(path.endswith(".dist-info/RECORD") for path, _ in verified.files)


def test_build_and_inspect_exact_local_version() -> None:
    version = "1.0.0+papertrade.1"
    verified = inspect_wheel(
        _wheel(version),
        distribution="team-strategy",
        version=version,
        requires_python=">=3.12,<3.13",
        required_modules=("team_strategy",),
    )
    assert b"Version: 1.0.0+papertrade.1\n" in verified.content(
        f"team_strategy-{version}.dist-info/METADATA"
    )


@pytest.mark.parametrize(
    "member",
    [
        "team_strategy-1.0.0.dev20261105+nt.papertrade.7.dist-info/WHEEL",
        "team_strategy-1.0.0.dev20261105+other.dist-info/WHEEL",
        "team_strategy/bad+name.py",
        "nested/team_strategy-1.0.0.dev20261105+nt.papertrade.7.dist-info/WHEEL",
    ],
)
def test_inspects_standard_candidate_metadata_without_releasing_other_plus_paths(member: str):
    version = "1.0.0.dev20261105+nt.papertrade.7"
    dist_info = f"team_strategy-{version}.dist-info"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Name: team-strategy\nVersion: {version}\nRequires-Python: >=3.12,<3.13\n",
        )
        archive.writestr("team_strategy/__init__.py", b"")
        archive.writestr(member, b"")
    arguments = {
        "distribution": "team-strategy",
        "version": version,
        "requires_python": ">=3.12,<3.13",
        "required_modules": ("team_strategy",),
    }
    if member == f"{dist_info}/WHEEL":
        assert inspect_wheel(output.getvalue(), **arguments).version == version
    else:
        with pytest.raises(WheelArchiveError, match="unsupported characters"):
            inspect_wheel(output.getvalue(), **arguments)


def _unsafe_wheel(
    path: str,
    content: bytes,
    *,
    mode: int = stat.S_IFREG | 0o644,
    requires_python: str = ">=3.12,<3.13",
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        metadata = zipfile.ZipInfo("fixture-1.0.0.dist-info/METADATA")
        metadata.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(
            metadata,
            f"Name: fixture\nVersion: 1.0.0\nRequires-Python: {requires_python}\n",
        )
        member = zipfile.ZipInfo(path)
        member.external_attr = mode << 16
        archive.writestr(member, content)
    return output.getvalue()


@pytest.mark.parametrize("path", ("../escape.py", "/absolute.py", "shared/runtime.py", "x.pth"))
def test_rejects_unsafe_or_legacy_members(path: str) -> None:
    wheel = _unsafe_wheel(path, b"unsafe")

    with pytest.raises(WheelArchiveError):
        inspect_wheel(
            wheel,
            distribution="fixture",
            version="1.0.0",
            requires_python=">=3.12,<3.13",
            required_modules=("fixture",),
        )


def test_rejects_symlink_member_before_import() -> None:
    wheel = _unsafe_wheel("fixture/link.py", b"target", mode=stat.S_IFLNK | 0o777)

    with pytest.raises(WheelArchiveError, match="symlink"):
        inspect_wheel(
            wheel,
            distribution="fixture",
            version="1.0.0",
            requires_python=">=3.12,<3.13",
            required_modules=("fixture",),
        )


def test_accepts_equivalent_python_compatibility_order() -> None:
    wheel = _unsafe_wheel(
        "fixture/__init__.py",
        b"",
        requires_python="<3.13,>=3.12",
    )

    inspect_wheel(
        wheel,
        distribution="fixture",
        version="1.0.0",
        requires_python=">=3.12,<3.13",
        required_modules=("fixture",),
    )


def test_rejects_different_python_compatibility() -> None:
    wheel = _unsafe_wheel(
        "fixture/__init__.py",
        b"",
        requires_python=">=3.12",
    )

    with pytest.raises(WheelArchiveError, match="compatibility"):
        inspect_wheel(
            wheel,
            distribution="fixture",
            version="1.0.0",
            requires_python=">=3.12,<3.13",
            required_modules=("fixture",),
        )
