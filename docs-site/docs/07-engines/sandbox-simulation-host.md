---
title: "Sandbox simulation host"
sidebar_position: 2
---

`--engine sandbox-sim` selects `SandboxSimulationHost`. It accepts sandbox only, holds no venue positions and makes no venue connection.

## What it exercises

On the signed lane, the surrounding runtime verifies and activates artifacts, resolves local credentials, applies lifecycle changes and publishes signed facts through its adapter. On the offline lane, the simulator exercises local desired-state delivery, vault resolution, attachment and unsigned status.

The simulator does not import or run a trading strategy. Use `--engine nautilus` with a compatible strategy to test strategy behavior against market data and local fills.

## Observations

Open notional is zero. Flattening is a logged no-op. A deployed simulation instance is ready without waiting for market data. The signed fact adapter supplies the signed-lane observation surface; offline output remains local and unsigned.

See [standalone sandbox](/getting-started/standalone-sandbox) for a runnable exercise and [execution admission](/concepts/live-execution-gate) for mode restrictions.
