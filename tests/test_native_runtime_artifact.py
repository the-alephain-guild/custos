"""Synthetic archives exercise measurement rules, never native runtime qualification."""

from __future__ import annotations

import hashlib
import py_compile
import sysconfig
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from custos.engines.nautilus import native_runtime_artifact as subject

VERSION = "2.0.0rc5+papertrade.7"
TAG = "cp312-cp312-manylinux_2_39_x86_64"
DIST_INFO = f"nautilus_trader-{VERSION}.dist-info"
NATIVE = f"nautilus_trader/_libnautilus{sysconfig.get_config_var('EXT_SUFFIX')}"


@pytest.fixture
def installation(tmp_path, monkeypatch):
    prefix = tmp_path / "venv"
    root = prefix / "site-packages"
    contents = {
        "nautilus_trader/__init__.py": b"# synthetic package\n",
        "nautilus_trader/live/__init__.py": b"# synthetic live wrapper\n",
        "nautilus_trader/adapters/sandbox/__init__.py": b"# synthetic sandbox wrapper\n",
        NATIVE: b"synthetic native payload; never imported\n",
        f"{DIST_INFO}/METADATA": (
            f"Metadata-Version: 2.1\nName: nautilus-trader\nVersion: {VERSION}\n"
            "Requires-Python: >=3.11,<4\n\n"
        ).encode(),
        f"{DIST_INFO}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {TAG}\n\n".encode(),
    }
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    wheel = tmp_path / f"nautilus_trader-{VERSION}-{TAG}.whl"

    def archive():
        with zipfile.ZipFile(wheel, "w") as output:
            for name, data in contents.items():
                output.writestr(name, data)
        return hashlib.sha256(wheel.read_bytes()).hexdigest()

    module_paths = (
        ("nautilus_trader", root / "nautilus_trader/__init__.py"),
        ("nautilus_trader.live", root / "nautilus_trader/live/__init__.py"),
        ("nautilus_trader.adapters.sandbox", root / "nautilus_trader/adapters/sandbox/__init__.py"),
        ("nautilus_trader._libnautilus", root / NATIVE),
    )
    observed = subject.InstalledNativeRuntime(
        distribution_version=VERSION,
        package_version=VERSION,
        native_package_version="2.0.0rc5",
        distribution_root=root,
        environment_root=prefix,
        module_paths=module_paths,
    )
    monkeypatch.setattr(subject, "_observe_installation", lambda: observed)
    monkeypatch.setattr(subject, "_supported_tags", lambda: frozenset({TAG}))
    return wheel, root, contents, archive, observed


async def measure(installation, **changes):
    wheel, _, _, archive, _ = installation
    arguments = {"archive_sha256": archive(), "distribution_version": VERSION, **changes}
    return await subject.measure_native_runtime_archive(wheel, **arguments)


async def test_matching_archive_and_installed_payload_produce_immutable_measurement(installation):
    measured = await measure(installation)
    assert measured.distribution_version == VERSION
    assert measured.native_package_version == "2.0.0rc5"
    assert measured.archive_bytes == installation[0].stat().st_size
    assert len(measured.payload_files) == len(installation[2])
    with pytest.raises(FrozenInstanceError):
        measured.distribution_version = "forged"


@pytest.mark.parametrize("digest", ["f" * 64, "sha256:" + "f" * 64, "not-a-digest"])
async def test_archive_digest_is_measured_instead_of_assumed(installation, digest):
    with pytest.raises(subject.NativeRuntimeMeasurementError):
        await measure(installation, archive_sha256=digest)


@pytest.mark.parametrize(
    "path", [NATIVE, "nautilus_trader/live/__init__.py", f"{DIST_INFO}/METADATA"]
)
async def test_changed_installed_payload_is_refused(installation, path):
    wheel, root, _, archive, _ = installation
    expected = archive()
    (root / path).write_bytes(b"changed installed bytes")
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="payload"):
        await subject.measure_native_runtime_archive(
            wheel, archive_sha256=expected, distribution_version=VERSION
        )


async def test_other_installed_distribution_version_is_refused(installation):
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="version"):
        await measure(installation, distribution_version="2.0.0rc5+sodex.3")


async def test_incompatible_abi_is_refused(installation, monkeypatch):
    monkeypatch.setattr(subject, "_supported_tags", lambda: frozenset({"cp311-cp311-any"}))
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="tag"):
        await measure(installation)


@pytest.mark.parametrize("member", ["nautilus_trader/../escape.py", "/outside.py"])
async def test_archive_paths_are_closed_without_extracting(installation, member):
    wheel, _, _, archive, _ = installation
    archive()
    with zipfile.ZipFile(wheel, "a") as output:
        output.writestr(member, b"escape")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="path"):
        await subject.measure_native_runtime_archive(
            wheel, archive_sha256=digest, distribution_version=VERSION
        )


async def test_extra_installed_executable_is_not_hidden_by_record(installation):
    (installation[1] / "nautilus_trader/foreign.py").write_text("# foreign\n")
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="extra"):
        await measure(installation)


async def test_installed_payload_symlink_is_refused(installation):
    path = installation[1] / NATIVE
    outside = path.parent.parent / "outside.so"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="symlink"):
        await measure(installation)


async def test_archive_mutation_during_measurement_is_refused(installation, monkeypatch):
    wheel, _, _, archive, _ = installation
    digest = archive()
    original_hash = subject._hash
    first = True

    def append_after_read(stream, *args):
        nonlocal first
        result = original_hash(stream, *args)
        if first:
            first = False
            with wheel.open("ab") as output:
                output.write(b"changed after digest")
        return result

    monkeypatch.setattr(subject, "_hash", append_after_read)
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="changed"):
        await subject.measure_native_runtime_archive(
            wheel, archive_sha256=digest, distribution_version=VERSION
        )


async def test_duplicate_archive_payload_is_refused(installation):
    wheel, _, _, archive, _ = installation
    archive()
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(wheel, "a") as output:
        output.writestr(NATIVE, b"another payload")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="duplicate"):
        await subject.measure_native_runtime_archive(
            wheel, archive_sha256=digest, distribution_version=VERSION
        )


@pytest.mark.parametrize("suffix", ["py", "so", "pyc"])
async def test_cache_directory_cannot_hide_foreign_executable(installation, suffix):
    extra = installation[1] / "nautilus_trader/__pycache__" / f"foreign.{suffix}"
    extra.parent.mkdir()
    extra.write_bytes(b"foreign executable")
    with pytest.raises(subject.NativeRuntimeMeasurementError):
        await measure(installation)


@pytest.mark.parametrize("optimization", [0, 1, 2])
@pytest.mark.parametrize("invalidation", list(py_compile.PycInvalidationMode))
async def test_bytecode_is_checked_against_archived_source(
    installation, optimization, invalidation
):
    source = installation[1] / "nautilus_trader/live/__init__.py"
    py_compile.compile(
        str(source), doraise=True, optimize=optimization, invalidation_mode=invalidation
    )
    measured = await measure(installation)
    assert len(measured.bytecode_files) == 1
    assert measured.bytecode_files[0].source_member == "nautilus_trader/live/__init__.py"


async def test_matching_header_cannot_hide_changed_bytecode(installation):
    source = installation[1] / "nautilus_trader/live/__init__.py"
    cache = py_compile.compile(str(source), doraise=True)
    path = Path(cache)
    original = path.read_bytes()
    path.write_bytes(original[:16] + b"foreign code with the original valid header")
    with pytest.raises(subject.NativeRuntimeMeasurementError, match="bytecode payload"):
        await measure(installation)
