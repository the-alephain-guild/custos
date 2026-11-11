"""Candidate packages are rebuilt from fixed source without changing released inputs."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "native-capture-darwin7-v1"
VERSION = "0.1.0.dev20261105+nt.papertrade.7"


def _head(repository: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()


@pytest.fixture(scope="module")
def source_repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Commit actual build inputs while keeping the developer checkout untouched."""
    from scripts.build_native_candidate import SOURCE_PATHS

    repository = tmp_path_factory.mktemp("native-candidate-source")
    for relative in SOURCE_PATHS:
        original = ROOT / relative
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_dir():
            shutil.copytree(
                original,
                target,
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        else:
            shutil.copy2(original, target, follow_symlinks=False)
    for arguments in (
        ["init", "-q"],
        ["config", "user.name", "Candidate source fixture"],
        ["config", "user.email", "candidate@example.invalid"],
        ["add", "--", *SOURCE_PATHS],
        [
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Current candidate source fixture",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return repository


def test_fixed_source_export_has_truthful_metadata_without_touching_released_inputs(
    source_repository, tmp_path
):
    from scripts.build_native_candidate import export_candidate_source

    protected = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    protected += list((ROOT / "docs/authority/receipts").rglob("*.json"))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    exported = export_candidate_source(
        repository_root=source_repository,
        source_commit=_head(source_repository),
        profile_id=PROFILE,
        output_root=tmp_path / "candidate",
    )
    source = exported.source_root
    base = tomllib.loads((source / "packages/custos-strategy-toolkit/pyproject.toml").read_text())
    adapter = tomllib.loads(
        (source / "packages/custos-strategy-toolkit-nautilus/pyproject.toml").read_text()
    )
    runner = tomllib.loads((source / "pyproject.toml").read_text())
    publisher = tomllib.loads(
        (source / "packages/custos-strategy-publisher/pyproject.toml").read_text()
    )
    assert base["project"]["version"] == adapter["project"]["version"] == VERSION
    assert runner["project"]["version"] == "0.8.0.dev20261105+nt.papertrade.7"
    assert publisher["project"]["version"] == VERSION
    assert f"custos-strategy-toolkit=={VERSION}" in runner["project"]["dependencies"]
    assert (
        f"custos-strategy-toolkit-nautilus=={VERSION}"
        in (runner["project"]["optional-dependencies"]["nautilus"])
    )
    assert "nautilus-trader==2.0.0rc5+papertrade.7" in adapter["project"]["dependencies"]
    assert [
        r
        for r in publisher["project"]["optional-dependencies"]["build"]
        if r.startswith("nautilus-trader")
    ] == ["nautilus-trader==2.0.0rc5+papertrade.7"]
    assert (source / "src/custos/engines/nautilus/native_runtime_artifact.py").read_bytes() == (
        ROOT / "src/custos/engines/nautilus/native_runtime_artifact.py"
    ).read_bytes()
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    assert not (tmp_path / "candidate/candidate-source-profile.json").exists()


def test_export_recipe_cannot_be_changed_through_a_mutable_alias(source_repository, tmp_path):
    from scripts.build_native_candidate import export_candidate_source

    exported = export_candidate_source(
        repository_root=source_repository,
        source_commit=_head(source_repository),
        profile_id=PROFILE,
        output_root=tmp_path / "candidate",
    )
    recipe = exported.recipe
    recipe["versions"]["custos-strategy-toolkit"] = "0.1.0"
    recipe["engine"]["version"] = "2.0.0rc5+sodex.3"
    assert exported.recipe["versions"]["custos-strategy-toolkit"] == VERSION
    assert exported.recipe["engine"]["version"] == "2.0.0rc5+papertrade.7"


def test_four_candidate_wheels_have_real_metadata_and_static_engine_payloads(
    source_repository, tmp_path
):
    from scripts.build_native_candidate import build_candidate_wheels, export_candidate_source

    exported = export_candidate_source(
        repository_root=source_repository,
        source_commit=_head(source_repository),
        profile_id=PROFILE,
        output_root=tmp_path / "candidate",
    )
    wheels = build_candidate_wheels(
        exported,
        python=Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True
            ).strip()
        )
        / ".venv/bin/python",
    )
    assert set(wheels) == {
        "custos-runner",
        "custos-strategy-toolkit",
        "custos-strategy-toolkit-nautilus",
        "custos-strategy-publisher",
    }
    with zipfile.ZipFile(tmp_path / "candidate" / wheels["custos-runner"]["wheel_path"]) as runner:
        embedded = json.loads(runner.read("custos/core/_native_build_descriptor_v1.json"))
    assert embedded == {
        "schema_version": 1,
        "profile_id": PROFILE,
        "source_commit": exported.source_commit,
        "engine": exported.recipe["engine"],
    }
    for name, descriptor in wheels.items():
        assert descriptor["name"] == name
        assert descriptor["version"] == exported.recipe["versions"][name]
        assert (tmp_path / "candidate" / descriptor["wheel_path"]).is_file()
    assert f"custos-strategy-toolkit=={VERSION}" in wheels["custos-runner"]["requires_dist"]
    assert (
        "nautilus-trader==2.0.0rc5+papertrade.7"
        in wheels["custos-strategy-toolkit-nautilus"]["requires_dist"]
    )
    assert not (tmp_path / "candidate/candidate-source-profile.json").exists()


@pytest.mark.integration
def test_actual_native_archive_installs_with_all_four_candidate_packages(
    source_repository, tmp_path
):
    archive = os.environ.get("CUSTOS_NATIVE_CANDIDATE_WHEEL")
    if archive is None:
        pytest.skip("an actual qualified native candidate archive must be provided explicitly")
    from scripts.build_native_candidate import build_native_candidate

    output = tmp_path / "runtime-candidate"
    profile = build_native_candidate(
        repository_root=source_repository,
        source_commit=_head(source_repository),
        profile_id=PROFILE,
        native_wheel=Path(archive),
        python=ROOT / ".venv/bin/python",
        output_root=output,
    )
    for relative in ("candidate-source-profile.json", "source-inventory.json"):
        raw = (output / relative).read_bytes()
        assert (
            raw
            == json.dumps(
                json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT / "packaging/runtime-profiles/candidate-source-profile-v1.schema.json").read_bytes()
    )
    Draft202012Validator(schema).validate(profile)
    assert profile["source_commit"] == _head(source_repository)
    assert profile["source_version"] == VERSION
    assert profile["engine"]["abi"] == "cp312"
    assert profile["acceptance"] == {
        "local_candidate": True,
        "rc_published": False,
        "control_ready": False,
    }
    assert (output / "runtime/.venv/bin/python").is_file()
    assert (output / "runtime/uv.lock").is_file()
    assets = json.loads((output / "candidate-contract-assets.json").read_bytes())
    assert assets["engine_version"] == profile["engine"]["version"]
    assert assets["fixture_version_mapping"] == {
        "base_contracts_version": "1.0.0rc1",
        "engine_toolkit_version": "1.0.0rc1",
    }
    for relative, descriptor in assets["assets"].items():
        raw = (output / relative).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == descriptor["sha256"]
        assert len(raw) == descriptor["size_bytes"]
        assert "/receipts/" not in relative and "/vendor/" not in relative
    for name in profile["distributions"]:
        command = json.loads((output / f"build-{name}.command.json").read_bytes())
        assert command["exit_code"] == 0
        assert "--offline" in command["argv"]
    installed = json.loads((output / "installed-payloads.log").read_text())
    assert installed["installed_payloads_match"] is True
    assert installed["profile_id"] == PROFILE
    subprocess.run(
        [
            str(output / "runtime/.venv/bin/python"),
            "-I",
            "-B",
            "-c",
            "import sys; from pathlib import Path; "
            "import custos.core.platform_process_fence as fence; "
            "import custos.engines.nautilus.platform_account_identity as identity; "
            "from nautilus_trader.adapters.sodex import SodexExecClientConfig; "
            "assert Path(fence.__file__).resolve().is_relative_to(Path(sys.prefix)); "
            "assert Path(identity.__file__).resolve().is_relative_to(Path(sys.prefix)); "
            "config = SodexExecClientConfig(account_id=123, wallet_address='0x'+'11'*20); "
            "assert config.account_id == 123; "
            "assert config.wallet_address == '0x'+'11'*20; "
            "assert not hasattr(config, 'api_private_key'); "
            "import asyncio; from custos.core.native_runtime_bundle import measure_candidate_native_bundle; "
            "measured = asyncio.run(measure_candidate_native_bundle(Path.cwd())); "
            f"assert measured.artifact_digest == {profile['engine']['artifact_sha256']!r}; "
            f"assert measured.crate_version == {profile['engine']['native_build']['crate_version']!r}",
        ],
        cwd=output,
        check=True,
        capture_output=True,
    )


@pytest.mark.integration
def test_previous_same_version_archive_is_refused_before_source_export(source_repository, tmp_path):
    previous = os.environ.get("CUSTOS_PREVIOUS_NATIVE_CANDIDATE_WHEEL")
    if previous is None:
        pytest.skip("the previous same-version archive must be provided explicitly")
    from scripts.build_native_candidate import build_native_candidate

    output = tmp_path / "previous-candidate"
    with pytest.raises(ValueError, match="native archive digest differs"):
        build_native_candidate(
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            native_wheel=Path(previous),
            python=ROOT / ".venv/bin/python",
            output_root=output,
        )
    assert not output.exists()


def test_an_output_inside_an_existing_virtual_environment_is_refused(source_repository, tmp_path):
    from scripts.build_native_candidate import export_candidate_source

    environment = tmp_path / "existing-environment"
    environment.mkdir()
    marker = environment / "pyvenv.cfg"
    marker.write_text("include-system-site-packages = false\n")
    with pytest.raises(ValueError, match="existing virtual environment"):
        export_candidate_source(
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            output_root=environment / "candidate",
        )
    assert not (environment / "candidate").exists()


def test_a_recipe_cannot_reuse_a_released_distribution_version(source_repository, tmp_path):
    tool = tmp_path / "tool"
    (tool / "scripts").mkdir(parents=True)
    (tool / "packaging/runtime-profiles").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/build_native_candidate.py", tool / "scripts/build_native_candidate.py"
    )
    data = json.loads((ROOT / f"packaging/runtime-profiles/{PROFILE}.json").read_bytes())
    data["versions"]["custos-strategy-toolkit"] = "0.1.0"
    (tool / f"packaging/runtime-profiles/{PROFILE}.json").write_text(json.dumps(data))
    api = runpy.run_path(str(tool / "scripts/build_native_candidate.py"))
    with pytest.raises(ValueError, match="candidate distribution version"):
        api["export_candidate_source"](
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            output_root=tmp_path / "candidate",
        )
    assert not (tmp_path / "candidate").exists()


@pytest.mark.parametrize("digest", [None, 3, []])
def test_malformed_recipe_digest_is_rejected_before_export(source_repository, tmp_path, digest):
    tool = tmp_path / "tool"
    (tool / "scripts").mkdir(parents=True)
    (tool / "packaging/runtime-profiles").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/build_native_candidate.py", tool / "scripts/build_native_candidate.py"
    )
    recipe = json.loads((ROOT / f"packaging/runtime-profiles/{PROFILE}.json").read_bytes())
    recipe["engine"]["artifact_sha256"] = digest
    (tool / f"packaging/runtime-profiles/{PROFILE}.json").write_text(json.dumps(recipe))
    api = runpy.run_path(str(tool / "scripts/build_native_candidate.py"))
    with pytest.raises(ValueError, match="recipe"):
        api["export_candidate_source"](
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            output_root=tmp_path / "candidate",
        )
    assert not (tmp_path / "candidate").exists()


@pytest.mark.integration
def test_real_native_archive_cannot_be_renamed_to_another_engine(source_repository, tmp_path):
    archive = os.environ.get("CUSTOS_NATIVE_CANDIDATE_WHEEL")
    if archive is None:
        pytest.skip("an actual qualified native candidate archive must be provided explicitly")
    from scripts.build_native_candidate import build_native_candidate

    renamed = tmp_path / "nautilus_trader-2.0.0rc5+sodex.3-cp312-cp312-macosx_11_0_arm64.whl"
    os.link(archive, renamed)
    with pytest.raises(ValueError, match="native archive filename"):
        build_native_candidate(
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            native_wheel=renamed,
            python=ROOT / ".venv/bin/python",
            output_root=tmp_path / "candidate",
        )
    assert not (tmp_path / "candidate").exists()


def test_built_metadata_cannot_retain_a_released_cross_package_pin(source_repository, tmp_path):
    from scripts.build_native_candidate import build_candidate_wheels, export_candidate_source

    exported = export_candidate_source(
        repository_root=source_repository,
        source_commit=_head(source_repository),
        profile_id=PROFILE,
        output_root=tmp_path / "candidate",
    )
    project = exported.source_root / "pyproject.toml"
    project.write_text(
        project.read_text().replace(
            f"custos-strategy-toolkit=={VERSION}", "custos-strategy-toolkit==0.1.0"
        )
    )
    with pytest.raises(ValueError, match="built dependency"):
        build_candidate_wheels(exported, python=ROOT / ".venv/bin/python")
    assert not (tmp_path / "candidate/candidate-source-profile.json").exists()


@pytest.mark.parametrize(
    "profile,revision", [("unknown", None), ("released-sodex3-v1", None), (PROFILE, "HEAD")]
)
def test_export_requires_an_explicit_candidate_profile_and_exact_commit(
    source_repository, tmp_path, profile, revision
):
    from scripts.build_native_candidate import export_candidate_source

    with pytest.raises(ValueError, match="unknown candidate|exact Git"):
        export_candidate_source(
            repository_root=source_repository,
            source_commit=revision or _head(source_repository),
            profile_id=profile,
            output_root=tmp_path / "candidate",
        )
    assert not (tmp_path / "candidate").exists()


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "symlink"])
def test_source_export_rejects_dirty_inputs_and_committed_symlinks(
    source_repository, tmp_path, mutation
):
    from scripts.build_native_candidate import export_candidate_source

    repository = tmp_path / "repository"
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(source_repository), str(repository)], check=True
    )
    if mutation == "tracked":
        (repository / "README.md").write_text("changed source\n")
    elif mutation == "untracked":
        (repository / "src/unexpected_candidate.py").write_text("unexpected = True\n")
    else:
        (repository / "README.md").unlink()
        (repository / "README.md").symlink_to("LICENSE")
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Candidate test",
                "-c",
                "user.email=candidate@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "test: committed symlink",
            ],
            cwd=repository,
            check=True,
        )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    with pytest.raises(ValueError, match="clean Git|non-regular source"):
        export_candidate_source(
            repository_root=repository,
            source_commit=revision,
            profile_id=PROFILE,
            output_root=tmp_path / "candidate",
        )
    assert not (tmp_path / "candidate").exists()


def test_output_cannot_overwrite_existing_files_or_enter_the_checkout(source_repository, tmp_path):
    from scripts.build_native_candidate import export_candidate_source

    marker = tmp_path / "candidate"
    marker.write_text("existing data")
    with pytest.raises(FileExistsError, match="must be new"):
        export_candidate_source(
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            output_root=marker,
        )
    assert marker.read_text() == "existing data"
    with pytest.raises(ValueError, match="outside the source checkout"):
        export_candidate_source(
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            output_root=source_repository / "candidate-output",
        )


@pytest.mark.parametrize("mutation", ["duplicate", "noncanonical"])
def test_recipe_rejects_ambiguous_or_noncanonical_identity(source_repository, tmp_path, mutation):
    tool = tmp_path / "tool"
    (tool / "scripts").mkdir(parents=True)
    (tool / "packaging/runtime-profiles").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/build_native_candidate.py", tool / "scripts/build_native_candidate.py"
    )
    raw = (ROOT / f"packaging/runtime-profiles/{PROFILE}.json").read_text()
    if mutation == "duplicate":
        raw = raw.replace('"schema_version": 1,', '"schema_version": 0, "schema_version": 1,')
    else:
        raw = raw.replace(VERSION, "0.1.0-dev20261105+NT.papertrade.7")
    (tool / f"packaging/runtime-profiles/{PROFILE}.json").write_text(raw)
    api = runpy.run_path(str(tool / "scripts/build_native_candidate.py"))
    with pytest.raises(ValueError, match="duplicate|canonical"):
        api["export_candidate_source"](
            repository_root=source_repository,
            source_commit=_head(source_repository),
            profile_id=PROFILE,
            output_root=tmp_path / "candidate",
        )
    assert not (tmp_path / "candidate").exists()
