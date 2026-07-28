"""Reproducible-build hook (Plan 12 T8).

hatchling >= 1.20 honours ``SOURCE_DATE_EPOCH`` natively when producing the
wheel, so this hook is intentionally minimal. Its purpose is defence-in-depth:

- Log at build time whether ``SOURCE_DATE_EPOCH`` is set so an operator running
  ``uv build`` outside CI can see immediately whether the reproducibility knob
  was engaged.
- Give a stable table entry ``[tool.hatch.build.hooks.custom]`` in
  ``pyproject.toml`` so a future hatchling downgrade (or a hatchling behaviour
  change that opts out of SOURCE_DATE_EPOCH) has an obvious place to grow a
  real implementation without a further pyproject edit.

External audit invariant: this hook must never modify wheel content — the
whole point of reproducibility is that the wheel is a deterministic function
of the source tree + the epoch. See the signed release chain chapter.
"""

from __future__ import annotations

import os
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ReproducibleBuildHook(BuildHookInterface):
    """No-op build hook that surfaces the SOURCE_DATE_EPOCH signal."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if epoch:
            print(
                f"[custos hatch-hook] reproducible build engaged: "
                f"SOURCE_DATE_EPOCH={epoch}",
                file=sys.stderr,
            )
        else:
            print(
                "[custos hatch-hook] SOURCE_DATE_EPOCH not set; "
                "wheel mtime will follow host clock",
                file=sys.stderr,
            )
