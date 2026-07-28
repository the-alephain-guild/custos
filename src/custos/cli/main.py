"""Legacy entry-point stub — the flat CLI has been removed.

The old ``python -m custos --tenant-id X --runner-id Y ...`` surface was
replaced by the ``arx-runner`` subcommand dispatcher (``arx-runner enroll`` /
``arx-runner vault {put,verify,list}`` / ``arx-runner start``). This stub
survives so ``python -m custos`` returns a clear, actionable error rather than
``ModuleNotFoundError``.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print(
        "custos: the `python -m custos` / `custos` entry point has been removed. "
        "Use `arx-runner start` instead; run `arx-runner --help` for the available "
        "subcommands, or see https://custos.alephain.com/getting-started/installation.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
