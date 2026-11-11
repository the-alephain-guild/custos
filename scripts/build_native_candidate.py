"""Rebuild an explicit local candidate from fixed Git source and a closed recipe."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = "native-capture-darwin7-v1"
PROJECTS = {
    "custos-runner": Path("."),
    "custos-strategy-toolkit": Path("packages/custos-strategy-toolkit"),
    "custos-strategy-toolkit-nautilus": Path("packages/custos-strategy-toolkit-nautilus"),
    "custos-strategy-publisher": Path("packages/custos-strategy-publisher"),
}
SOURCE_PATHS = (
    "src",
    "packages/custos-strategy-toolkit",
    "packages/custos-strategy-toolkit-nautilus",
    "packages/custos-strategy-publisher",
    "pyproject.toml",
    "uv.lock",
    "hatch_build.py",
    "LICENSE",
    "README.md",
    "scripts/native_candidate_profile.py",
    "scripts/generate_strategy_contract_assets.py",
)


@dataclass(frozen=True)
class CandidateSource:
    source_root: Path
    source_commit: str
    source_date_epoch: int
    _recipe_json: bytes

    @property
    def recipe(self) -> dict:
        return json.loads(self._recipe_json)


def _run(argv: list[str], *, cwd: Path, binary: bool = False) -> str | bytes:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=not binary, check=False)
    if result.returncode:
        raise ValueError(f"{argv[0]} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("candidate recipe contains a duplicate field")
        result[key] = value
    return result


def _recipe(profile_id: str) -> dict:
    from custos.core.native_runtime_bundle import validate_native_engine

    if profile_id != PROFILE_ID:
        raise ValueError("unknown candidate build profile")
    path = ROOT / "packaging/runtime-profiles" / f"{profile_id}.json"
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("engine"), dict)
        or not isinstance(value.get("versions"), dict)
    ):
        raise ValueError("candidate recipe must have closed object fields")
    if (
        set(value) != {"schema_version", "profile_id", "engine", "versions"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["profile_id"] != profile_id
        or set(value["versions"]) != set(PROJECTS)
        or set(value["engine"])
        != {"distribution", "version", "artifact_sha256", "abi", "platform", "native_build"}
        or value["engine"]["distribution"] != "nautilus-trader"
        or value["engine"]["abi"] != "cp312"
        or value["engine"]["platform"] != "macosx_11_0_arm64"
        or not isinstance(value["engine"]["artifact_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["engine"]["artifact_sha256"]) is None
    ):
        raise ValueError("candidate recipe has unknown fields or incompatible values")
    validate_native_engine(value["engine"])
    try:
        versions = [Version(v) for v in value["versions"].values()]
        if any(
            str(parsed) != raw
            for parsed, raw in zip(versions, value["versions"].values(), strict=True)
        ):
            raise ValueError("candidate distribution version must use its canonical spelling")
        if any(not v.is_devrelease or v.local != "nt.papertrade.7" for v in versions):
            raise ValueError("candidate distribution version must be an explicit local dev build")
    except (InvalidVersion, TypeError):
        raise ValueError("invalid candidate distribution version") from None
    if (
        value["versions"]["custos-strategy-toolkit"]
        != value["versions"]["custos-strategy-toolkit-nautilus"]
    ):
        raise ValueError("candidate toolkit distribution versions must match")
    return value


def _rewrite_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError("source metadata differs from the declared transform")
    return text.replace(old, new)


def _project_metadata(path: Path, name: str, recipe: dict) -> None:
    text = path.read_text()
    original = tomllib.loads(text)
    project = original["project"]
    if project["name"] != name:
        raise ValueError("source project identity differs")
    if Version(recipe["versions"][name]).base_version != project["version"]:
        raise ValueError("candidate version does not match the source distribution base version")
    changed = json.loads(json.dumps(original))
    changed["project"]["version"] = recipe["versions"][name]
    text = _rewrite_once(
        text,
        f"version = {json.dumps(project['version'])}",
        f"version = {json.dumps(recipe['versions'][name])}",
    )
    for field in ("dependencies",):
        for dependency in project.get(field, []):
            package = dependency.partition("==")[0]
            if package in recipe["versions"]:
                updated = f"{package}=={recipe['versions'][package]}"
            elif package == "nautilus-trader":
                if dependency != "nautilus-trader==2.0.0rc5+sodex.3":
                    raise ValueError("source engine pin differs")
                updated = f"nautilus-trader=={recipe['engine']['version']}"
            else:
                continue
            text = _rewrite_once(text, json.dumps(dependency), json.dumps(updated))
            changed["project"][field] = [
                updated if item == dependency else item for item in changed["project"][field]
            ]
    for extra, dependencies in project.get("optional-dependencies", {}).items():
        for dependency in dependencies:
            package = dependency.partition("==")[0]
            if package in recipe["versions"]:
                updated = f"{package}=={recipe['versions'][package]}"
                text = _rewrite_once(text, json.dumps(dependency), json.dumps(updated))
                changed["project"]["optional-dependencies"][extra] = [
                    updated if item == dependency else item
                    for item in changed["project"]["optional-dependencies"][extra]
                ]
        native = [r for r in dependencies if r.startswith("nautilus-trader @ ")]
        if native:
            if name != "custos-strategy-publisher" or extra != "build" or len(native) != 3:
                raise ValueError("unrecognized native source dependency")
            for index, dependency in enumerate(native):
                replacement = (
                    json.dumps(f"nautilus-trader=={recipe['engine']['version']}") + ","
                    if index == 0
                    else ""
                )
                text = _rewrite_once(text, json.dumps(dependency) + ",", replacement)
            changed["project"]["optional-dependencies"][extra] = [
                r for r in dependencies if r not in native
            ] + [f"nautilus-trader=={recipe['engine']['version']}"]
    rendered = tomllib.loads(text)
    if rendered != changed:
        raise ValueError("metadata rendering changed an undeclared field")
    path.write_text(text)


def export_candidate_source(
    *, repository_root: Path, source_commit: str, profile_id: str, output_root: Path
) -> CandidateSource:
    """Export committed inputs and apply only the declared metadata/static-definition transforms."""
    recipe = _recipe(profile_id)
    repository_root = repository_root.resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit):
        raise ValueError("source_commit must be an exact Git commit")
    resolved = _run(
        ["git", "rev-parse", "--verify", f"{source_commit}^{{commit}}"], cwd=repository_root
    ).strip()
    if resolved != source_commit:
        raise ValueError("source_commit differs from the requested commit")
    dirty = subprocess.run(
        ["git", "diff", "--quiet", source_commit, "--", *SOURCE_PATHS], cwd=repository_root
    )
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *SOURCE_PATHS],
        cwd=repository_root,
    ).strip()
    if dirty.returncode or untracked:
        raise ValueError("exported source inputs must exactly match the clean Git commit")
    output_root = output_root.absolute()
    if output_root.resolve().is_relative_to(repository_root):
        raise ValueError("candidate output must be outside the source checkout")
    if output_root.exists() or output_root.is_symlink():
        raise FileExistsError("candidate output must be new")
    if any((parent / "pyvenv.cfg").exists() for parent in output_root.parents):
        raise ValueError("candidate output cannot be inside an existing virtual environment")
    archive = _run(
        ["git", "archive", "--format=tar", source_commit, *SOURCE_PATHS],
        cwd=repository_root,
        binary=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        members = tar.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Git export contains an unsafe path or non-regular source")
        source = output_root / "source"
        source.mkdir(parents=True)
        tar.extractall(source, filter="data")
    renderer = runpy.run_path(str(source / "scripts/native_candidate_profile.py"))[
        "render_engine_definitions"
    ]
    for relative, expected in renderer("released-sodex3-v1").items():
        if (source / relative).read_bytes() != expected:
            raise ValueError("source definitions are not the released build profile")
    for relative, content in renderer(profile_id).items():
        version = next(
            ast.literal_eval(node.value)
            for node in ast.parse(content).body
            if isinstance(node, ast.AnnAssign) and node.target.id == "ENGINE_VERSION"
        )
        if version != recipe["engine"]["version"]:
            raise ValueError("recipe engine differs from the static definition")
        (source / relative).write_bytes(content)
    for name, relative in PROJECTS.items():
        _project_metadata(source / relative / "pyproject.toml", name, recipe)
    from custos.core.native_runtime_bundle import descriptor_bytes

    (source / "src/custos/core/_native_build_descriptor_v1.json").write_bytes(
        descriptor_bytes(recipe, source_commit)
    )
    epoch = int(_run(["git", "show", "-s", "--format=%ct", source_commit], cwd=repository_root))
    return CandidateSource(source, source_commit, epoch, _canonical(recipe))


def _build_environment(epoch: int) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment.update(
        SOURCE_DATE_EPOCH=str(epoch),
        PYTHONHASHSEED="0",
        PYTHONDONTWRITEBYTECODE="1",
        UV_OFFLINE="1",
        UV_NO_PYTHON_DOWNLOADS="1",
        VIRTUAL_ENV="",
        CARGO_BUILD_JOBS="2",
    )
    return environment


def _archive_metadata(path: Path, output: Path, *, expected_tag: str | None = None) -> dict:
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        with zipfile.ZipFile(stream) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or archive.testzip() is not None:
                raise ValueError("wheel contains duplicate members or invalid CRC")
            metadata_paths = [n for n in names if n.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise ValueError("wheel has an ambiguous metadata member")
            raw = archive.read(metadata_paths[0])
            metadata = BytesParser().parsebytes(raw)
            if expected_tag is not None:
                wheel_paths = [n for n in names if n.endswith(".dist-info/WHEEL")]
                if len(wheel_paths) != 1 or BytesParser().parsebytes(
                    archive.read(wheel_paths[0])
                ).get_all("Tag") != [expected_tag]:
                    raise ValueError("native wheel tags differ from the candidate recipe")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        current = path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(before, field) != getattr(after, field)
            or getattr(after, field) != getattr(current, field)
            for field in fields
        ):
            raise ValueError("wheel changed during archive verification")
    return {
        "name": metadata["Name"],
        "version": metadata["Version"],
        "wheel_path": path.relative_to(output).as_posix(),
        "sha256": digest,
        "size_bytes": after.st_size,
        "metadata_sha256": hashlib.sha256(raw).hexdigest(),
        "requires_python": metadata["Requires-Python"],
        "requires_dist": metadata.get_all("Requires-Dist") or [],
    }


def build_candidate_wheels(exported: CandidateSource, *, python: Path) -> dict[str, dict]:
    """Build real archives from the staged projects; this does not grant runtime authority."""
    output = exported.source_root.parent
    dist = output / "wheels"
    dist.mkdir()
    environment = _build_environment(exported.source_date_epoch)
    result = {}
    for name, relative in PROJECTS.items():
        command = [
            "uv",
            "build",
            "--offline",
            "--no-config",
            "--no-create-gitignore",
            "--wheel",
            "--python",
            str(python.absolute()),
            "--no-python-downloads",
            "--out-dir",
            str(dist),
            str(exported.source_root / relative),
        ]
        _step(command, cwd=output, environment=environment, log=output / f"build-{name}.log")
        paths = list(dist.glob(f"{name.replace('-', '_')}-*.whl"))
        if len(paths) != 1:
            raise ValueError("candidate build did not produce one exact distribution")
        descriptor = _archive_metadata(paths[0], output)
        if descriptor["name"] != name or descriptor["version"] != exported.recipe["versions"][name]:
            raise ValueError("built metadata differs from the candidate recipe")
        expected_pins = {
            **exported.recipe["versions"],
            "nautilus-trader": exported.recipe["engine"]["version"],
        }
        for text in descriptor["requires_dist"]:
            requirement = Requirement(text)
            dependency = canonicalize_name(requirement.name)
            if dependency in expected_pins and (
                requirement.url is not None
                or [(s.operator, s.version) for s in requirement.specifier]
                != [("==", expected_pins[dependency])]
            ):
                raise ValueError("built dependency differs from the candidate recipe")
        result[name] = descriptor
    return result


def _candidate_assets(exported: CandidateSource, python: Path, environment: dict) -> None:
    output = exported.source_root.parent
    program = r"""
import hashlib,json,runpy,sys
from pathlib import Path,PurePosixPath
source=Path(sys.argv[1]);out=Path(sys.argv[2]);sys.path[:0]=[str(source/'packages/custos-strategy-toolkit/src')]
generator=runpy.run_path(str(source/'scripts/generate_strategy_contract_assets.py'))
assets=generator['build_standalone_contract_assets']()
from custos_toolkit.contracts.engine_profile import ENGINE_PROFILE_ID,ENGINE_VERSION
rows={}
for relative,content in assets.items():
    p=PurePosixPath(relative)
    assert not p.is_absolute() and '..' not in p.parts and '/receipts/' not in relative and '/vendor/' not in relative
    target=out/'contract-assets'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
    rows['contract-assets/'+relative]={'sha256':hashlib.sha256(content).hexdigest(),'size_bytes':len(content)}
ref=json.loads(assets['docs/authority/strategy-artifact-ref-v1.golden.json'])['artifact_ref']
report={'schema_version':1,'profile_id':ENGINE_PROFILE_ID,'engine_version':ENGINE_VERSION,'status':'LOCAL_CANDIDATE_OWNER_ASSETS_ONLY','assets':rows,'fixture_version_mapping':{k:ref[k] for k in ['base_contracts_version','engine_toolkit_version']},'deferred':['peer pre-import golden requires its own candidate owner','published toolkit RC authority is not created by local packaging']}
(out/'candidate-contract-assets.json').write_text(json.dumps(report,sort_keys=True,separators=(',',':')))
print(json.dumps({'asset_count':len(rows),'engine_version':ENGINE_VERSION}))
"""
    _step(
        [str(python), "-I", "-B", "-c", program, str(exported.source_root), str(output)],
        cwd=output,
        environment=environment,
        log=output / "contract-assets.log",
    )


def _inventory(source: Path) -> dict[str, dict]:
    inventory = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("source export contains a non-regular entry")
        if path.is_file():
            inventory[path.relative_to(source).as_posix()] = {
                "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
                "size_bytes": path.stat().st_size,
            }
    return inventory


def _step(command: list[str], *, cwd: Path, environment: dict, log: Path) -> None:
    completed = subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
    )
    log.write_text(completed.stdout + completed.stderr)
    log.with_suffix(".command.json").write_bytes(
        _canonical({"argv": command, "cwd": str(cwd), "exit_code": completed.returncode})
    )
    if completed.returncode:
        raise ValueError(f"candidate step {log.name} failed ({completed.returncode}); see {log}")


def _native_input(path: Path, recipe: dict) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("native archive must be a regular file")
    engine = recipe["engine"]
    expected_name = (
        f"nautilus_trader-{engine['version']}-cp312-{engine['abi']}-{engine['platform']}.whl"
    )
    if path.name != expected_name:
        raise ValueError("native archive filename differs from the candidate recipe")
    descriptor = _archive_metadata(
        path, path.parent, expected_tag=f"cp312-{engine['abi']}-{engine['platform']}"
    )
    if descriptor["sha256"] != engine["artifact_sha256"]:
        raise ValueError("native archive digest differs from the approved candidate recipe")
    if descriptor["name"] != engine["distribution"] or descriptor["version"] != engine["version"]:
        raise ValueError("native metadata differs from the candidate recipe")
    return descriptor


def _runtime_project(output: Path, wheels: dict, native: dict, recipe: dict) -> Path:
    project = output / "runtime"
    project.mkdir()
    dependencies = [f"{name}=={row['version']}" for name, row in wheels.items()]
    dependencies.append(f"nautilus-trader=={native['version']}")
    text = (
        '[project]\nname = "custos-native-candidate-runtime"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12,<3.13"\n'
        f"dependencies = {json.dumps(dependencies)}\n\n"
        '[tool.uv]\npackage = false\nprerelease = "allow"\n'
        "environments = [\"sys_platform == 'darwin' and platform_machine == 'arm64'\"]\n"
        "required-environments = [\"sys_platform == 'darwin' and platform_machine == 'arm64'\"]\n"
        "\n[tool.uv.sources]\n"
    )
    for name, row in {**wheels, "nautilus-trader": native}.items():
        text += f"{name} = {{path = {json.dumps('../' + row['wheel_path'])}}}\n"
    (project / "pyproject.toml").write_text(text)
    return project


def build_native_candidate(
    *,
    repository_root: Path,
    source_commit: str,
    profile_id: str,
    native_wheel: Path,
    python: Path,
    output_root: Path,
) -> dict:
    """Build and install a local candidate; never create an RC or runtime admission receipt."""
    recipe = _recipe(profile_id)
    _native_input(native_wheel, recipe)
    python = python.absolute()
    version = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-B",
                "-c",
                "import json,sys;print(json.dumps(list(sys.version_info[:2])))",
            ]
        )
    )
    if version != [3, 12] or sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("this candidate requires a Darwin CPython 3.12 build environment")
    exported = export_candidate_source(
        repository_root=repository_root,
        source_commit=source_commit,
        profile_id=profile_id,
        output_root=output_root,
    )
    if exported.recipe != recipe:
        raise ValueError("candidate recipe changed during source validation")
    output = exported.source_root.parent
    inventory = _inventory(exported.source_root)
    native_dir = output / "native"
    native_dir.mkdir()
    native_path = native_dir / native_wheel.name
    shutil.copyfile(native_wheel, native_path)
    native = _native_input(native_path, recipe)
    native["wheel_path"] = native_path.relative_to(output).as_posix()
    wheels = build_candidate_wheels(exported, python=python)
    environment = _build_environment(exported.source_date_epoch)
    _candidate_assets(exported, python, environment)
    project = _runtime_project(output, wheels, native, recipe)
    shutil.copyfile(exported.source_root / "uv.lock", project / "uv.lock")
    for argv, name in (
        (["uv", "lock", "--offline", "--python", str(python), "--no-python-downloads"], "lock.log"),
        (
            [
                "uv",
                "sync",
                "--locked",
                "--offline",
                "--python",
                str(python),
                "--no-python-downloads",
                "--no-dev",
            ],
            "sync.log",
        ),
        (["uv", "pip", "check", "--python", str(project / ".venv/bin/python")], "pip-check.log"),
    ):
        _step(argv, cwd=project, environment=environment, log=output / name)
    descriptors = {**wheels, "nautilus-trader": native}
    (output / "archive-inputs.json").write_bytes(_canonical(descriptors))
    verify = r"""
import hashlib,importlib,importlib.metadata as metadata,json,sys,zipfile
from pathlib import Path,PurePosixPath
output=Path(sys.argv[1]); descriptors=json.loads((output/'archive-inputs.json').read_bytes())
for name,row in descriptors.items():
    distribution=metadata.distribution(name)
    assert distribution.version==row['version'],name
    with zipfile.ZipFile(output/row['wheel_path']) as wheel:
        for member in wheel.namelist():
            path=PurePosixPath(member)
            assert not path.is_absolute() and '..' not in path.parts,member
            if member.endswith('/') or member.endswith('.dist-info/RECORD'): continue
            installed=Path(distribution.locate_file(member))
            assert installed.resolve().is_relative_to(Path(sys.prefix).resolve()),member
            with wheel.open(member) as source,installed.open('rb') as actual:
                assert hashlib.file_digest(source,'sha256').digest()==hashlib.file_digest(actual,'sha256').digest(),member
for module in ['custos','custos_toolkit','custos_toolkit_nautilus','custos_strategy_publisher','nautilus_trader']:
    loaded=importlib.import_module(module)
    assert Path(loaded.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),module
from custos_toolkit.contracts.engine_profile import ENGINE_VERSION,ENGINE_PROFILE_ID
from custos_toolkit.contracts.strategy_execution import StrategyManifestV1,StrategyArtifactRefV1
from custos_strategy_publisher.model import ENGINE_VERSION as PUBLISHER_ENGINE_VERSION
from custos_strategy_publisher.engine_profile import ENGINE_PROFILE_ID as PUBLISHER_PROFILE_ID
from pydantic import TypeAdapter,ValidationError
from custos_toolkit.contracts.engine_profile import EngineVersion
from custos.core.native_runtime_bundle import _installed_descriptor
descriptor = _installed_descriptor()
assert descriptor['source_commit']==sys.argv[3]
assert descriptor['engine']['artifact_sha256']==descriptors['nautilus-trader']['sha256']
assert descriptor['profile_id']==sys.argv[2]
assert ENGINE_VERSION==PUBLISHER_ENGINE_VERSION==descriptors['nautilus-trader']['version']
assert ENGINE_PROFILE_ID==PUBLISHER_PROFILE_ID==sys.argv[2]
for model in [StrategyManifestV1,StrategyArtifactRefV1]:
    assert model.model_json_schema()['properties']['engine_version']['const']==ENGINE_VERSION
assert TypeAdapter(EngineVersion).validate_python(ENGINE_VERSION)==ENGINE_VERSION
try: TypeAdapter(EngineVersion).validate_python('2.0.0rc5+sodex.3')
except ValidationError: pass
else: raise AssertionError('candidate parser accepted released engine identity')
print(json.dumps({'installed_payloads_match':True,'engine_version':ENGINE_VERSION,'profile_id':ENGINE_PROFILE_ID,'prefix':sys.prefix}))
"""
    _step(
        [
            str(project / ".venv/bin/python"),
            "-I",
            "-B",
            "-c",
            verify,
            str(output),
            profile_id,
            source_commit,
        ],
        cwd=output,
        environment=environment,
        log=output / "installed-payloads.log",
    )
    if _inventory(exported.source_root) != inventory:
        raise ValueError("candidate source changed during build or installation")
    base = wheels["custos-strategy-toolkit"]["version"]
    if wheels["custos-strategy-toolkit-nautilus"]["version"] != base:
        raise ValueError("candidate toolkit versions differ")
    definition_paths = runpy.run_path(
        str(exported.source_root / "scripts/native_candidate_profile.py")
    )["render_engine_definitions"](profile_id)
    profile = {
        "schema_version": "alephain.custos.native-candidate-source-profile.v1",
        "profile_id": profile_id,
        "source_repository": "https://github.com/the-alephain-guild/custos",
        "source_commit": source_commit,
        "source_date_epoch": exported.source_date_epoch,
        "source_root": "source",
        "source_inventory_path": "source-inventory.json",
        "source_inventory_sha256": hashlib.sha256(_canonical(inventory)).hexdigest(),
        "source_version": base,
        "engine": {
            "distribution": native["name"],
            "version": native["version"],
            "artifact_sha256": native["sha256"],
            "wheel_path": native["wheel_path"],
            "abi": recipe["engine"]["abi"],
            "platform": recipe["engine"]["platform"],
            "native_build": recipe["engine"]["native_build"],
        },
        "distributions": wheels,
        "engine_definitions": {
            "source/" + relative: {
                "sha256": hashlib.sha256((exported.source_root / relative).read_bytes()).hexdigest()
            }
            for relative in definition_paths
        },
        "acceptance": {"local_candidate": True, "rc_published": False, "control_ready": False},
    }
    (output / "source-inventory.json").write_bytes(_canonical(inventory))
    tool_inputs = {}
    for relative in (
        "scripts/build_native_candidate.py",
        f"packaging/runtime-profiles/{profile_id}.json",
        "packaging/runtime-profiles/candidate-source-profile-v1.schema.json",
    ):
        content = (ROOT / relative).read_bytes()
        tool_inputs[relative] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    (output / "build-tool-inputs.json").write_bytes(_canonical(tool_inputs))
    (output / "candidate-source-profile.json").write_bytes(_canonical(profile))
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--native-wheel", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        build_native_candidate(
            repository_root=ROOT,
            source_commit=args.source_commit,
            profile_id=args.profile,
            native_wheel=args.native_wheel,
            python=args.python,
            output_root=args.out,
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(args.out / "candidate-source-profile.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
