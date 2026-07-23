from __future__ import annotations

import hashlib
import stat
import sys
import unicodedata
import zipfile

import pytest

from custos.artifacts.archive import quarantine_wheel
from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.policy import ArchiveLimitsV1
from tests._artifact_archive_fixtures import regular_zip_info, write_test_wheel


def test_valid_wheel_is_quarantined_without_importing_entry_point(tmp_path) -> None:
    wheel = tmp_path / "supertrend.whl"
    write_test_wheel(wheel)

    verified = quarantine_wheel(
        wheel,
        entry_point_group="alephain.strategy_runtime.v1",
        entry_point="strategies.supertrend:RuntimeAdapter",
        limits=ArchiveLimitsV1(),
        quarantine_parent=tmp_path / "quarantine",
    )

    assert verified.root.is_dir()
    assert verified.verified_entry_point == "strategies.supertrend:RuntimeAdapter"
    assert "strategies.supertrend" not in sys.modules
    assert (verified.root / "strategies/supertrend.py").is_file()


def test_required_runtime_artifact_is_verified_after_safe_extraction(tmp_path) -> None:
    wheel = tmp_path / "supertrend.whl"
    schema = b'{"type":"object"}'
    write_test_wheel(
        wheel,
        extra_entries=[
            (
                regular_zip_info("strategies/resources/config.schema.json"),
                schema,
            )
        ],
    )

    verified = quarantine_wheel(
        wheel,
        entry_point_group="alephain.strategy_runtime.v1",
        entry_point="strategies.supertrend:RuntimeAdapter",
        limits=ArchiveLimitsV1(),
        quarantine_parent=tmp_path / "quarantine",
        required_runtime_artifacts=(
            {
                "role": "runtime_artifact",
                "name": "resources/config.schema.json",
                "media_type": "application/schema+json",
                "size_bytes": len(schema),
                "sha256": hashlib.sha256(schema).hexdigest(),
            },
        ),
    )

    assert verified.root.is_dir()


def test_required_runtime_artifact_drift_fails_closed(tmp_path) -> None:
    wheel = tmp_path / "supertrend.whl"
    schema = b'{"type":"object"}'
    write_test_wheel(
        wheel,
        extra_entries=[
            (
                regular_zip_info("strategies/resources/config.schema.json"),
                schema,
            )
        ],
    )

    with pytest.raises(ArtifactVerificationError) as error:
        quarantine_wheel(
            wheel,
            entry_point_group="alephain.strategy_runtime.v1",
            entry_point="strategies.supertrend:RuntimeAdapter",
            limits=ArchiveLimitsV1(),
            quarantine_parent=tmp_path / "quarantine",
            required_runtime_artifacts=(
                {
                    "role": "runtime_artifact",
                    "name": "resources/config.schema.json",
                    "media_type": "application/schema+json",
                    "size_bytes": len(schema),
                    "sha256": "0" * 64,
                },
            ),
        )

    assert error.value.code is ArtifactVerificationCode.MEMBER_SET_MISMATCH


@pytest.mark.parametrize("case", ["traversal", "symlink", "device", "normalization", "duplicate"])
def test_unsafe_archive_members_are_rejected_and_quarantine_is_removed(tmp_path, case: str) -> None:
    wheel = tmp_path / f"{case}.whl"
    extras: list[tuple[zipfile.ZipInfo, bytes]] = []
    if case == "traversal":
        extras.append((regular_zip_info("../escape.py"), b"escape"))
    elif case == "symlink":
        info = regular_zip_info("strategies/link.py")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        extras.append((info, b"/etc/passwd"))
    elif case == "device":
        info = regular_zip_info("strategies/device")
        info.external_attr = (stat.S_IFCHR | 0o600) << 16
        extras.append((info, b""))
    elif case == "normalization":
        composed = unicodedata.normalize("NFC", "strategies/cafe\u0301.py")
        decomposed = unicodedata.normalize("NFD", composed)
        extras.extend(
            [(regular_zip_info(composed), b"x=1\n"), (regular_zip_info(decomposed), b"x=2\n")]
        )
    else:
        extras.append((regular_zip_info("strategies/supertrend.py"), b"duplicate"))
    if case == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            write_test_wheel(wheel, extra_entries=extras)
    else:
        write_test_wheel(wheel, extra_entries=extras)
    quarantine = tmp_path / "quarantine"

    with pytest.raises(ArtifactVerificationError) as error:
        quarantine_wheel(
            wheel,
            entry_point_group="alephain.strategy_runtime.v1",
            entry_point="strategies.supertrend:RuntimeAdapter",
            limits=ArchiveLimitsV1(),
            quarantine_parent=quarantine,
        )

    assert error.value.code is ArtifactVerificationCode.ARCHIVE_UNSAFE
    assert not quarantine.exists() or not any(quarantine.iterdir())


def test_archive_limits_reject_compression_bomb(tmp_path) -> None:
    wheel = tmp_path / "bomb.whl"
    write_test_wheel(
        wheel,
        module_payload=b"0" * 100_000,
        compression=zipfile.ZIP_DEFLATED,
    )
    limits = ArchiveLimitsV1(max_compression_ratio=2)

    with pytest.raises(ArtifactVerificationError) as error:
        quarantine_wheel(
            wheel,
            entry_point_group="alephain.strategy_runtime.v1",
            entry_point="strategies.supertrend:RuntimeAdapter",
            limits=limits,
            quarantine_parent=tmp_path / "quarantine",
        )

    assert error.value.code is ArtifactVerificationCode.ARCHIVE_LIMIT_EXCEEDED


def test_crc_and_record_mismatch_fail_closed(tmp_path) -> None:
    record_bad = tmp_path / "record-bad.whl"
    write_test_wheel(record_bad, record_digest_override="sha256=" + "A" * 43)
    with pytest.raises(ArtifactVerificationError) as record_error:
        quarantine_wheel(
            record_bad,
            entry_point_group="alephain.strategy_runtime.v1",
            entry_point="strategies.supertrend:RuntimeAdapter",
            limits=ArchiveLimitsV1(),
            quarantine_parent=tmp_path / "record-quarantine",
        )
    assert record_error.value.code is ArtifactVerificationCode.WHEEL_RECORD_INVALID

    crc_bad = tmp_path / "crc-bad.whl"
    write_test_wheel(crc_bad)
    raw = crc_bad.read_bytes()
    original = b"class RuntimeAdapter:\n    pass\n"
    assert raw.count(original) == 1
    crc_bad.write_bytes(raw.replace(original, b"class RuntimeAdapter:\n    fail\n", 1))
    with pytest.raises(ArtifactVerificationError) as crc_error:
        quarantine_wheel(
            crc_bad,
            entry_point_group="alephain.strategy_runtime.v1",
            entry_point="strategies.supertrend:RuntimeAdapter",
            limits=ArchiveLimitsV1(),
            quarantine_parent=tmp_path / "crc-quarantine",
        )
    assert crc_error.value.code is ArtifactVerificationCode.ARCHIVE_CRC_MISMATCH


def test_entry_point_must_exist_in_metadata_and_python_ast(tmp_path) -> None:
    wheel = tmp_path / "missing-entrypoint.whl"
    write_test_wheel(wheel, module_payload=b"class SomethingElse:\n    pass\n")

    with pytest.raises(ArtifactVerificationError) as error:
        quarantine_wheel(
            wheel,
            entry_point_group="alephain.strategy_runtime.v1",
            entry_point="strategies.supertrend:RuntimeAdapter",
            limits=ArchiveLimitsV1(),
            quarantine_parent=tmp_path / "quarantine",
        )

    assert error.value.code is ArtifactVerificationCode.ENTRY_POINT_INVALID
