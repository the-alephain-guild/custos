"""A strategy made from the strategy template needs no release configuration.

Its names come from its directory and pyproject, its source inventory from what
is under refinement/, and the runtime adapter generated for it is the one the
first producer shipped, with only the names changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from example_producer import PS_SUPERTREND

from custos_strategy_publisher.artifact_build import (
    ArtifactBuildError,
    ProducerIdentity,
    StrategyReleaseSpec,
    _runtime_module,
)
from custos_strategy_publisher.template import (
    template_source_inventory,
    template_strategy_spec,
)

GOLDENS = Path(__file__).parent / "goldens"
STRATEGY = "strategies/trend/supertrend"
PRODUCER = ProducerIdentity(
    repository="example-owner/example-strategies",
    ghcr_repository="ghcr.io/example-owner/example-strategies/strategy-releases",
    workflow_identity=(
        "https://github.com/the-alephain-guild/custos/.github/workflows/"
        "publish-strategy-release.yml@refs/tags/v0.4.0"
    ),
)


def _strategy(root: Path, *, name: str = "supertrend", registers: str = "supertrend") -> Path:
    strategy = root / "strategies" / "trend" / name
    nautilus = strategy / "refinement" / "nautilus"
    nautilus.mkdir(parents=True)
    (strategy / "pyproject.toml").write_text(
        f'[project]\nname = "strategy-{name}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (strategy / "config.yaml").write_text("strategy: {}\n", encoding="utf-8")
    (strategy / "README.md").write_text("not released\n", encoding="utf-8")
    (strategy / "refinement" / "__init__.py").write_text("", encoding="utf-8")
    (nautilus / "__init__.py").write_text("", encoding="utf-8")
    (nautilus / "strategy.py").write_text(
        "from custos_toolkit_nautilus.adapter import register_strategy\n\n"
        f'register_strategy(name="{registers}", strategy_class=object)\n',
        encoding="utf-8",
    )
    (nautilus / "notes.txt").write_text("kept out of the wheel\n", encoding="utf-8")
    cache = nautilus / "__pycache__"
    cache.mkdir()
    (cache / "strategy.cpython-312.pyc").write_bytes(b"\0")
    return strategy


def test_the_runtime_adapter_matches_the_first_producer_byte_for_byte() -> None:
    spec = StrategyReleaseSpec(**PS_SUPERTREND)  # type: ignore[arg-type]

    golden = (GOLDENS / "philosophers-stone-supertrend-runtime.py.txt").read_bytes()
    assert _runtime_module(spec) == golden


def test_names_come_from_the_directory_the_pyproject_and_the_producer(tmp_path: Path) -> None:
    _strategy(tmp_path)

    spec = template_strategy_spec(tmp_path, STRATEGY, PRODUCER)

    home = "github.com/example-owner/example-strategies"
    assert spec.registered_name == spec.catalog_alias == "supertrend"
    assert (spec.distribution, spec.version) == ("strategy-supertrend", "0.1.0")
    assert spec.package == "strategy_supertrend"
    assert spec.entry_point == "strategy_supertrend.runtime:RuntimeAdapterV1"
    assert spec.coordinate == f"strategy://{home}/trend/supertrend@0.1.0"
    assert spec.member_coordinate_prefix == f"artifact://{home}/strategy/trend/supertrend/0.1.0"
    assert spec.sbom_namespace == f"https://{home}/spdx/trend/supertrend"
    assert spec.config_schema_title == "SupertrendEffectiveConfigV1"
    assert spec.characterization_parameters is None


def test_the_inventory_selects_python_and_excludes_everything_else(tmp_path: Path) -> None:
    _strategy(tmp_path)
    spec = template_strategy_spec(tmp_path, STRATEGY, PRODUCER)

    inventory = template_source_inventory(tmp_path, STRATEGY, spec)
    snapshot = inventory.freeze()

    assert inventory.spec.selected_sources == (
        f"{STRATEGY}/pyproject.toml",
        f"{STRATEGY}/config.yaml",
        f"{STRATEGY}/refinement/__init__.py",
        f"{STRATEGY}/refinement/nautilus/__init__.py",
        f"{STRATEGY}/refinement/nautilus/strategy.py",
    )
    assert inventory.spec.excluded_sources == (f"{STRATEGY}/refinement/nautilus/notes.txt",)
    assert {member.path for member in snapshot.members} == {
        "pyproject.toml",
        "config.yaml",
        "refinement/__init__.py",
        "refinement/nautilus/__init__.py",
        "refinement/nautilus/strategy.py",
    }


def test_a_file_added_after_derivation_breaks_the_freeze(tmp_path: Path) -> None:
    strategy = _strategy(tmp_path)
    spec = template_strategy_spec(tmp_path, STRATEGY, PRODUCER)
    inventory = template_source_inventory(tmp_path, STRATEGY, spec)

    (strategy / "refinement" / "late.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(Exception, match="late.py"):
        inventory.freeze()


def test_a_strategy_registered_under_another_name_is_refused(tmp_path: Path) -> None:
    _strategy(tmp_path, registers="other")

    with pytest.raises(ArtifactBuildError, match="must match its directory name"):
        template_strategy_spec(tmp_path, STRATEGY, PRODUCER)


@pytest.mark.parametrize(
    "strategy",
    ["strategies/supertrend", "elsewhere/trend/supertrend", "strategies/../trend/x", "/abs/a/b"],
)
def test_only_the_template_layout_is_accepted(tmp_path: Path, strategy: str) -> None:
    with pytest.raises(ArtifactBuildError, match="strategies/<category>/<name>"):
        template_strategy_spec(tmp_path, strategy, PRODUCER)


def test_a_pyproject_without_a_version_is_refused(tmp_path: Path) -> None:
    strategy = _strategy(tmp_path)
    (strategy / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

    with pytest.raises(ArtifactBuildError, match="name and version"):
        template_strategy_spec(tmp_path, STRATEGY, PRODUCER)
