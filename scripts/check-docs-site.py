"""Check or regenerate public reference tables from the actual runner surface."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs-site"
ZH = json.loads((SITE / "data/reference-labels.json").read_text())["zh-Hans"]
LOCALES = (
    (SITE / "docs", False),
    (SITE / "i18n/zh-Hans/docusaurus-plugin-content-docs/current", True),
)


def parser_inventory() -> argparse.ArgumentParser:
    # Public tables describe clean defaults, never the operator's environment.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CUSTOS_", "CRUCIBLE_", "SOPS_"))
    }
    with patch.dict(os.environ, environment, clear=True):
        from custos.cli.subcommands import _build_parser

        return _build_parser()


def code(value: object) -> str:
    return "`" + str(value).replace("|", "\\|").replace("\n", " ") + "`"


def default_text(value: object) -> str:
    if value is None or value == "" or value == argparse.SUPPRESS:
        return "—"
    if isinstance(value, Path):
        return code(str(value).replace(str(Path.home()), "~", 1))
    return code(value)


def cli_table(parser: argparse.ArgumentParser, zh: bool) -> str:
    lines: list[str] = []

    def visit(current: argparse.ArgumentParser, path: str) -> None:
        if path:
            lines.extend([f"### {path}", ""])
            actions = [a for a in current._actions if a.option_strings and a.dest != "help"]
            if actions:
                lines.extend(
                    [
                        ZH["options"]
                        if zh
                        else "| Option | Requirement | Default | Choices / repetition |",
                        "|---|---|---|---|",
                    ]
                )
                for action in actions:
                    group_required = any(
                        group.required and action in group._group_actions
                        for group in current._mutually_exclusive_groups
                    )
                    required = (
                        (ZH["required"] if zh else "required")
                        if action.required
                        else (ZH["one_in_group"] if zh else "one in group")
                        if group_required
                        else (ZH["optional"] if zh else "optional")
                    )
                    values = ", ".join(code(v) for v in action.choices or ()) or "—"
                    if isinstance(action, argparse._AppendAction):
                        values += "; " + (ZH["repeatable"] if zh else "repeatable")
                    lines.append(
                        "| "
                        + " / ".join(code(s) for s in action.option_strings)
                        + f" | {required} | {default_text(action.default)} | {values} |"
                    )
                lines.append("")
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child, f"{path} {name}".strip())

    visit(parser, "")
    return "\n".join(
        line + " <!-- disclosure-ok: exact public CLI flag or default path from argparse -->"
        if "crucible" in line.lower()
        else line
        for line in lines
    ).strip()


def venue_table(zh: bool) -> str:
    tree = ast.parse((ROOT / "src/custos/engines/nautilus/host.py").read_text())
    groups = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "_SANDBOX_VENUES",
                    "_TESTNET_VENUES",
                    "_LIVE_VENUES",
                }:
                    groups[target.id] = set(ast.literal_eval(node.value.args[0]))
    if len(groups) != 3:
        raise ValueError("Cannot read the host's three venue declarations")
    lines = ["| Connector | sandbox | testnet | live |", "|---|---|---|---|"]
    yes, no = (ZH["declared"], ZH["unsupported"]) if zh else ("declared", "unsupported")
    for name in sorted(set.union(*groups.values())):
        cells = [
            yes if name in groups[f"_{mode}_VENUES"] else no
            for mode in ("SANDBOX", "TESTNET", "LIVE")
        ]
        lines.append(f"| {code(name)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def package_table(zh: bool) -> str:
    lines = [
        ZH["packages"] if zh else "| Package | Source version | Python |",
        "|---|---|---|",
    ]
    for folder in ("custos-strategy-toolkit", "custos-strategy-toolkit-nautilus"):
        metadata = tomllib.loads((ROOT / "packages" / folder / "pyproject.toml").read_text())
        project = metadata["project"]
        lines.append(
            f"| {code(project['name'])} | {code(project['version'])}"
            f" | {code(project['requires-python'])} |"
        )
    return "\n".join(lines)


def version_text(zh: bool) -> str:
    metadata = tomllib.loads(
        (ROOT / "packages/custos-strategy-toolkit-nautilus/pyproject.toml").read_text()
    )
    dependency = next(
        d for d in metadata["project"]["dependencies"] if d.startswith("nautilus-trader==")
    )
    return (ZH["dependency"] if zh else "Current dependency: ") + code(dependency) + "."


def schema_table(zh: bool) -> str:
    names = sorted(p.name for p in (ROOT / "docs/gateway-contract/v1").glob("*.schema.json"))
    label = ZH["schema"] if zh else "Schema file"
    return "\n".join([f"| {label} |", "|---|", *[f"| {code(n)} |" for n in names]])


def replace_block(text: str, name: str, value: str) -> str:
    pattern = rf"(<!-- generated:{re.escape(name)} -->).*?(<!-- /generated:{re.escape(name)} -->)"
    if len(re.findall(pattern, text, re.S)) != 1:
        raise ValueError(f"Expected exactly one generated block: {name}")
    return re.sub(pattern, lambda m: m[1] + "\n\n" + value + "\n\n" + m[2], text, flags=re.S)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Update generated reference blocks")
    args = parser.parse_args()
    actual_parser = parser_inventory()
    failures: list[str] = []
    blocks = {
        "09-reference/cli.md": {"cli": lambda zh: cli_table(actual_parser, zh)},
        "09-reference/json-schema.md": {"schemas": schema_table},
        "07-engines/nautilus-trader.md": {
            "venues": venue_table,
            "nautilus-version": version_text,
        },
        "08-toolkit/overview.md": {"packages": package_table},
    }
    for directory, zh in LOCALES:
        for relative, generators in blocks.items():
            path = directory / relative
            before = path.read_text()
            after = before
            for name, generate in generators.items():
                after = replace_block(after, name, generate(zh))
            if before != after:
                if args.write:
                    path.write_text(after)
                else:
                    failures.append(f"{path.relative_to(ROOT)}: generated reference drift")
        for path in directory.rglob("*.md"):
            for target in re.findall(r"`((?:src/|tests/|scripts/)[^`\s]+)`", path.read_text()):
                if "*" not in target and not (ROOT / target).exists():
                    failures.append(f"{path.relative_to(ROOT)}: missing source reference {target}")
    page_sets = [{p.relative_to(d) for p in d.rglob("*.md")} for d, _ in LOCALES]
    if page_sets[0] != page_sets[1]:
        failures.append(f"Locale page mismatch: {sorted(page_sets[0] ^ page_sets[1])}")
    # The complete signed quickstart must enable the command consumer.
    for directory, _ in LOCALES:
        quickstart = (directory / "02-getting-started/first-sandbox-run.md").read_text()
        examples = re.findall(r"```bash\n(.*?)```", quickstart, re.S)
        starts = [b for b in examples if "arx-runner start" in b]
        if not starts or any(not re.search(r"--reconcile(?:\s|$)", b) for b in starts):
            failures.append(f"{directory}: signed quickstart must enable --reconcile")
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        return 1
    print(f"Documentation references match source; {len(page_sets[0])} pages per locale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
