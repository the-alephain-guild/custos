"""Create non-trading fixtures for the documented sandbox-sim exercise."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    strategy = root / "strategy"
    strategy.mkdir(mode=0o700)
    (strategy / "README.txt").write_text(
        "Lifecycle fixture only. sandbox-sim never imports or trades this directory.\n"
    )
    # The runner reads the venue, pairs and leverage from the strategy's own
    # config.yaml; the spec does not carry them.
    (strategy / "config.yaml").write_text(
        "trading:\n"
        "  connector:\n    value: binance\n"
        "  pairs:\n    value: [BTC-USDT]\n"
        "  leverage:\n    value: 1\n"
    )
    spec = {
        "spec_id": "docs-sandbox",
        "generation": 1,
        "trading_mode": "sandbox",
        "lifecycle_state": "running",
        "strategy_path": str(strategy),
        "strategy_registry_name": "docs-simulation",
        "provenance_ref": {"credential_id": "docs-demo"},
        "sandbox": {"starting_balances": ["10000 USDT"]},
        "risk_config": {"max_total_notional": "200", "max_drawdown_pct": "0.05"},
    }
    for filename, changes in [
        ("spec.json", {}),
        ("stop.json", {"generation": 2, "lifecycle_state": "stopped"}),
    ]:
        with (root / filename).open("x") as target:
            json.dump({**spec, **changes}, target, indent=2)
            target.write("\n")
    print(root)


if __name__ == "__main__":
    main()
