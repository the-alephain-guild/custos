"""Document drift must fail when the executable surface changes independently."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "docs_site_reference_checker", ROOT / "scripts/check-docs-site.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    checker = load_checker()
    for folder in (
        "docs-site/docs",
        "docs-site/i18n/zh-Hans/docusaurus-plugin-content-docs/current",
        "docs/gateway-contract/v1",
        "packages/custos-strategy-toolkit",
        "packages/custos-strategy-toolkit-nautilus",
    ):
        source = ROOT / folder
        if folder.startswith("packages/"):
            target = tmp_path / folder
            target.mkdir(parents=True)
            shutil.copy(source / "pyproject.toml", target / "pyproject.toml")
        else:
            shutil.copytree(source, tmp_path / folder)
    for folder in ("src", "tests", "scripts"):
        shutil.copytree(
            ROOT / folder, tmp_path / folder, ignore=shutil.ignore_patterns("__pycache__")
        )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(
        checker,
        "LOCALES",
        tuple((tmp_path / path.relative_to(ROOT), zh) for path, zh in checker.LOCALES),
    )
    monkeypatch.setattr(sys, "argv", ["check-docs-site.py"])
    assert checker.main() == 0
    return checker


@pytest.mark.parametrize(
    "change", ["parser", "schema", "version", "venue", "translation", "source", "quickstart"]
)
def test_independent_drift_is_rejected(
    isolated_checker: ModuleType, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    checker = isolated_checker
    root = checker.ROOT
    if change == "parser":
        parser = checker.parser_inventory()
        commands = next(
            a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict)
        )
        commands.choices["start"].add_argument("--new-runtime-option")
        monkeypatch.setattr(checker, "parser_inventory", lambda: parser)
    elif change == "schema":
        (root / "docs/gateway-contract/v1/new_observation.schema.json").write_text("{}\n")
    elif change == "version":
        path = root / "packages/custos-strategy-toolkit-nautilus/pyproject.toml"
        path.write_text(path.read_text().replace("nautilus-trader==", "nautilus-trader==9."))
    elif change == "venue":
        path = root / "src/custos/engines/nautilus/host.py"
        path.write_text(
            path.read_text().replace(
                "_LIVE_VENUES = frozenset({", '_LIVE_VENUES = frozenset({"new_venue", '
            )
        )
    elif change == "translation":
        (checker.LOCALES[1][0] / "02-getting-started/standalone-sandbox.md").unlink()
    elif change == "source":
        (root / "tests/test_nt_venue_wiring.py").unlink()
    else:
        path = checker.LOCALES[0][0] / "02-getting-started/first-sandbox-run.md"
        path.write_text(path.read_text().replace("  --reconcile \\\n", ""))
    assert checker.main() == 1


def test_operator_environment_is_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    checker = load_checker()
    monkeypatch.setenv("CRUCIBLE_DOMAIN_EVENT_KEY_ID", "PRIVATE_SENTINEL")
    monkeypatch.setenv("CUSTOS_ARTIFACT_REGISTRY_TOKEN", "PRIVATE_SENTINEL")
    text = checker.cli_table(checker.parser_inventory(), False)
    assert "PRIVATE_SENTINEL" not in text


def test_signed_quickstart_commands_are_complete() -> None:
    checker = load_checker()
    values = {
        "TRANSPORT_AUTHORITY_URL": "https://transport.example.com",
        "TRANSPORT_INTENT_ID": "11111111-1111-4111-8111-111111111111",
        "NATS_SIM_URL": "tls://nats.example.com:4222",
    }
    for directory, _ in checker.LOCALES:
        text = (directory / "02-getting-started/first-sandbox-run.md").read_text()
        for block in re.findall(r"```bash\n(.*?)```", text, re.S):
            if "arx-runner start" not in block and "arx-runner nats-transport enroll" not in block:
                continue
            command = re.sub(r"\$([A-Z_]+)", lambda m: values.get(m[1], "/tmp/docs-input"), block)
            words = shlex.split(command.replace("\\\n", " "), comments=True)
            parsed = checker.parser_inventory().parse_args(words[words.index("arx-runner") + 1 :])
            if parsed.cmd == "start":
                assert parsed.reconcile is True


def test_standalone_fixture_uses_the_actual_offline_contract(tmp_path: Path) -> None:
    from custos.offline.spec import OfflineDeploymentSpec

    spec = importlib.util.spec_from_file_location(
        "docs_standalone_prepare", ROOT / "docs-site/examples/standalone/prepare.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from unittest.mock import patch

    with patch.object(sys, "argv", ["prepare.py", "--root", str(tmp_path)]):
        module.main()
    running = OfflineDeploymentSpec.model_validate(json.loads((tmp_path / "spec.json").read_text()))
    stopped = OfflineDeploymentSpec.model_validate(json.loads((tmp_path / "stop.json").read_text()))
    assert running.trading_mode.value == "sandbox"
    assert stopped.spec_id == running.spec_id
    assert stopped.generation > running.generation
    assert stopped.lifecycle_state.value == "stopped"
