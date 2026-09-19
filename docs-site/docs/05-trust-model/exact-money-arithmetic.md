---
title: "Decimal money arithmetic"
sidebar_position: 5
---

Custos uses `Decimal` at its typed money boundaries and canonical decimal strings for monetary wire values. Non-money timing values may use floats; they must not enter money calculations or signed fact payloads.

## Construct and serialize

Construct decimals from exact input strings. `Decimal("0.1")` represents 0.1 exactly, while `Decimal(0.1)` preserves the binary float approximation. Converting an already rounded float to a string cannot recover the original precision.

Money on the fact wire uses strings such as `"100.00"`; sequence/count fields remain integers. Consumers should parse money into an exact decimal type and apply the contract's scale/rounding rules where needed.

## Enforcement boundaries

Money fields in engine snapshots reject binary floats. Fact validation recursively rejects floats and non-finite values before durable persistence. Exposure, reservation and breaker code use decimal operations.

Relevant entry points are `src/custos/core/engine_protocol.py`, `src/custos/core/order_reservation_boundary.py`, `src/custos/core/fallback_breaker.py` and `src/custos/core/runner_fact.py`. Tests include `tests/test_nt_risk_engine.py` and `tests/test_runner_fact_store.py`.

Inspect upstream conversions as well as arithmetic: a Decimal result alone does not prove the input was exact. Correct representation also does not establish price freshness, reliable valuation or a profitable strategy.
