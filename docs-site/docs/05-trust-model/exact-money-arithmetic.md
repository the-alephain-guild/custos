---
title: "Money Arithmetic Is Exact"
sidebar_position: 5
---

# Money Arithmetic Is Exact

Every price, quantity and notional in Custos is a `Decimal`. On the wire they
are strings. No money value passes through a float anywhere in the runner.

## The failure it prevents

Floating point error does not announce itself.

`0.1 + 0.2` is not `0.3` in binary floating point. In a notional calculation
that difference produces a number that is almost right — it passes an eyeball
check, it passes a loosely-written test, and it is wrong in the direction of
the rounding. Accumulated over a position limit check, "almost right" is the
difference between a cap that holds and a cap that lets one more order through.

Decimal arithmetic removes the class of error rather than bounding it.

## The two rules

**Construct from strings.** `Decimal(str(x))`, never `Decimal(x)` where `x` is
a float. The second form silently inherits the binary error you were trying to
avoid: `Decimal(0.1)` is
`0.1000000000000000055511151231257827021181583404541015625`, while
`Decimal("0.1")` is exactly `0.1`.

This is the one that gets missed, because the two spellings look the same at a
glance and both produce a `Decimal`.

**Serialize as strings.** Money crosses the wire as `"100.00"`, not `100.0`.
A JSON number is a float to most parsers, so serialising a `Decimal` as a
number hands the problem to whoever reads it. Scale is preserved as written;
consumers quantize if they need to.

## Where it applies

The money paths are the ones where a wrong number becomes a wrong order or a
wrong limit:

| Path | Module |
|---|---|
| Aggregate exposure cap | `src/custos/core/local_cap.py` |
| Fallback breaker (drawdown, notional) | `src/custos/core/fallback_breaker.py` |
| Order reservation boundary | `src/custos/core/order_reservation_boundary.py` |
| Engine protocol surface | `src/custos/core/engine_protocol.py` |
| Signed fact production | `src/custos/core/runner_fact.py`, `runner_fact_producer.py` |
| Venue adapter | `src/custos/engines/nautilus/venue_binance.py` |

<!-- disclosure-ok: auditable source location -->

## Verifying it

```bash
grep -rnE 'float\(.*price|float\(.*amount|float\(.*notional' src/
```

Returns nothing on a clean tree.

That grep catches explicit conversion. The subtler case is `Decimal(x)` where
`x` arrived as a float — which is why the construction rule is worth checking
by reading rather than by pattern.

`tests/test_nt_risk_engine.py` exercises the breaker and cap arithmetic;
`tests/test_runner_fact_store.py` covers the durable and wire representation.
<!-- disclosure-ok: auditable source location -->

See the [audit checklist](./audit-checklist) for the full procedure.

## What this does not cover

Exact arithmetic means the number Custos computes is the number it meant. It
says nothing about whether that number is a good idea — a strategy can lose
money with perfect precision.
