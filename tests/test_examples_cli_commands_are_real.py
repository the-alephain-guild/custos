"""Example READMEs must only use flags the CLI actually accepts.

The existing example tests assert that certain command names appear in the
prose. That is a weaker property than it looks: a README can contain the string
``arx-runner vault put`` while the command below it is missing a required flag,
or passes one that does not exist. Both shipped that way -- the sandbox example
told operators to run ``--nats-url``, which argparse rejects outright, and both
examples omitted the required ``--scope-digest``.

So this checks the commands against the real parser rather than against a list
of expected substrings. An operator following an example verbatim is the whole
point of an example; a broken one is worse than none.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from custos.cli.subcommands import _build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_READMES = (
    REPO_ROOT / "examples" / "supertrend-sandbox" / "README.md",
    REPO_ROOT / "examples" / "supertrend-testnet" / "README.md",
)


def _documentation_pages() -> list[Path]:
    """Every published page, in both locales."""
    return sorted(
        list((REPO_ROOT / "docs-site" / "docs").rglob("*.md"))
        + list((REPO_ROOT / "docs-site" / "i18n").rglob("*.md"))
    )


# Both invocation forms a page may use: the installed console script, and the
# published image whose entrypoint is that same script. The subcommand must
# start with a letter so that `arx-runner --help` is not read as a subcommand
# named `--help`.
_INVOCATION = re.compile(
    r"(?:arx-runner|custos-runner:v[\d.]+)\s+"
    r"(vault\s+\w+|credential\s+\w+|nats-transport\s+\w+|[a-z][a-z-]*)"
)
_FLAG = re.compile(r"--[a-z][a-z-]*")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^\s*```")


def _invocation_body(lines: list[str], start: int) -> str:
    """Collect one command, following shell line continuations.

    A command ends at the first line that does not end in a backslash. Reading
    to the next blank line instead would swallow the commands that follow it in
    the same block, and report their flags against this one.
    """
    body: list[str] = []
    index = start
    while index < len(lines):
        body.append(lines[index])
        if not lines[index].rstrip().endswith("\\"):
            break
        index += 1
    return "\n".join(body)


def _subparser(name: str) -> argparse.ArgumentParser | None:
    """Resolve 'start' or 'vault put' to the parser that owns its flags."""
    parts = name.split()
    parser = _build_parser()
    for part in parts:
        actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not actions or part not in actions[0].choices:
            return None
        parser = actions[0].choices[part]
    return parser


def _accepted(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def _required(parser: argparse.ArgumentParser) -> set[str]:
    return {
        action.option_strings[0]
        for action in parser._actions
        if action.required and action.option_strings
    }


def _commands(text: str) -> list[tuple[str, set[str]]]:
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        match = _INVOCATION.search(line)
        if not match:
            continue
        # `arx-runner enroll` inside a sentence names the command; it is not an
        # invocation, and has no obligation to carry the required flags.
        if match.start() > 0 and line[match.start() - 1] == "`":
            continue
        name = " ".join(match.group(1).split())
        if _subparser(name) is None:
            continue
        body = _invocation_body(lines, index)[match.end() :]
        found.append((name, set(_FLAG.findall(body))))
    return found


def _named_subcommands(text: str) -> list[str]:
    """Every subcommand the page names, wherever a reader would copy it from.

    Only code counts -- a fenced block, or an inline span that begins with the
    tool name. Free prose is excluded so that an ordinary sentence containing
    the word `arx-runner` cannot be read as an invocation.

    This is deliberately separate from the flag checks above, which skip any
    name the parser does not recognise. Skipping was how `arx-runner deployment
    validate` survived on a published page for the entire life of the command's
    absence: an unknown flag was an error, but an entire unknown subcommand was
    treated as prose.
    """
    named: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        candidates = (
            [line]
            if fenced
            else [
                span
                for span in _INLINE_CODE.findall(line)
                if span.startswith(("arx-runner", "custos-runner:v"))
            ]
        )
        for candidate in candidates:
            match = _INVOCATION.search(candidate)
            if match:
                named.append(" ".join(match.group(1).split()))
    return named


def test_documentation_names_only_real_subcommands() -> None:
    offenders = []
    scanned = 0
    for page in _documentation_pages():
        for name in _named_subcommands(page.read_text(encoding="utf-8")):
            scanned += 1
            if _subparser(name) is None:
                offenders.append(f"{page.relative_to(REPO_ROOT)}: `arx-runner {name}`")

    # A regex that stops matching would leave this test passing on nothing.
    assert scanned > 50, (
        f"only {scanned} invocations found across the site — has the format changed?"
    )
    assert not offenders, "documented commands the CLI does not have:\n" + "\n".join(offenders)


def test_probe_detects_a_subcommand_that_does_not_exist() -> None:
    """The probe above is only worth having if it can fail."""
    page = "Run `arx-runner deployment validate --spec-file spec.json` first.\n"
    named = _named_subcommands(page)

    assert named == ["deployment"]
    assert _subparser("deployment") is None
    assert _named_subcommands("The `arx-runner` CLI is the only interface.\n") == []


@pytest.mark.parametrize("readme", EXAMPLE_READMES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_example_commands_use_only_real_flags(readme: Path) -> None:
    commands = _commands(readme.read_text(encoding="utf-8"))
    assert commands, f"no arx-runner commands found in {readme} — has the format changed?"

    for name, used in commands:
        accepted = _accepted(_subparser(name))
        unknown = sorted(flag for flag in used if flag not in accepted)
        assert not unknown, f"{readme.name}: `arx-runner {name}` passes unknown flag(s) {unknown}"


@pytest.mark.parametrize("readme", EXAMPLE_READMES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_example_commands_pass_every_required_flag(readme: Path) -> None:
    for name, used in _commands(readme.read_text(encoding="utf-8")):
        parser = _subparser(name)
        # A mutually exclusive required group is satisfied by any member, so
        # only single required options are checked here.
        missing = sorted(flag for flag in _required(parser) if flag not in used)
        assert not missing, f"{readme.name}: `arx-runner {name}` omits required flag(s) {missing}"


def test_documentation_uses_only_real_flags() -> None:
    """A flag that does not exist is wrong wherever it appears.

    Only unknown flags are checked here, not omitted required ones: a page may
    legitimately show `arx-runner start --engine nautilus` while discussing what
    `--engine` selects. An invented flag has no such excuse, and that is the
    failure that reached these pages -- renaming an internal name inside the
    published documentation produced `--arx-domain-public-key`, which argparse
    rejects.
    """
    offenders = []
    for page in _documentation_pages():
        for name, used in _commands(page.read_text(encoding="utf-8")):
            accepted = _accepted(_subparser(name))
            unknown = sorted(flag for flag in used if flag not in accepted)
            if unknown:
                offenders.append(f"{page.relative_to(REPO_ROOT)}: `{name}` {unknown}")

    assert not offenders, "documented commands pass flags the CLI does not accept:\n" + "\n".join(
        offenders
    )


def test_probe_detects_a_flag_that_does_not_exist() -> None:
    """The probe above is only worth having if it can fail."""
    parser = _subparser("start")
    assert parser is not None
    assert "--nats-sim-url" in _accepted(parser)
    assert "--nats-url" not in _accepted(parser), (
        "--nats-url was the flag the sandbox example wrongly documented; "
        "if it now exists this guard needs rethinking"
    )
