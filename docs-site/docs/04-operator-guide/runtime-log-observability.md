---
title: "Runtime Log & Observability"
sidebar_position: 4
---

# Runtime Log & Observability

You get observability in two separate channels, and the separation is the whole
design.

**Locally**, the runner writes structured JSON events to stdout. That is yours:
it stays on your machine, you decide what collects it, and it is as detailed as
the code makes it.

**Upstream**, the runner emits `RunnerRuntimeLogFact.v1` — a signed fact in the
same stream as every other fact it produces. That channel is verifiable, and
deliberately narrow.

Nothing bridges the two. The runner never tails its own stdout and ships it, and
never falls back to sending raw exception text.

## Why stdout is not shipped

A log line is unstructured by nature. Anything can end up in one — a credential
in an exception repr, a signed payload in a debug dump, an API response echoed
during a failure.

Shipping stdout would mean the credential guarantee depends on nobody ever
logging the wrong thing, anywhere, forever. That is not a guarantee; it is a
hope with a good track record until the day it isn't.

So the upstream channel accepts only explicitly constructed events, and every
one of them passes redaction before it is written anywhere durable.

## What a runtime-log fact contains

```json
{
  "kind": "RunnerRuntimeLogFact.v1",
  "event_id": "<deterministic uuidv5>",
  "occurred_at": "<RFC3339 UTC>",
  "level": "INFO",
  "component": "local_cap",
  "message": "…",
  "structured_fields": {},
  "correlation_id": "<uuid>",
  "causation_id": null
}
```

`level` is one of `DEBUG`, `INFO`, `WARN`, `ERROR` — a closed set. `component`
is supplied by the emitting code and must match
`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`.

The batch around it carries the full stream identity: tenant, mode, runner,
deployment instance, spec id and digest, generation, strategy, capability, key
id, a contiguous sequence range, payload digest and signature. Spec, digest and
generation are signed fences — they never change the subject and never reset the
sequence.

It travels on the ordinary fact subject and is verified exactly like a fill or a
settlement fact. See
[consuming RunnerFact](/integration/consuming-runner-fact).

## Redaction rejects, it does not scrub

Before a fact reaches the local queue, the redactor walks the message and every
structured field recursively:

- keys that look sensitive — API keys and secrets, passwords, tokens,
  credentials, authorization, private keys, age keys, KEKs — are anonymized;
- recognizable secret **shapes** are replaced wherever they appear: `Bearer`
  tokens, `rkc1` credentials, `AGE-SECRET-KEY-…`, PEM private keys,
  assignment-looking fragments, and high-entropy strings;
- anything still recognizable as secret material after that **rejects the entire
  fact**, before it touches SQLite.

That last point is the one worth internalizing. The redactor does not do its
best and pass the remainder through. A fact it cannot make safe does not become
a partially-redacted fact — it does not exist. Losing an observability event is
recoverable; publishing a credential is not.

The same applies to shapes it cannot represent: unsupported objects, non-finite
floats and binary floats are rejected recursively. Numeric fields travel as JSON
integers or canonical decimal strings, for the same reason money does.

## Limits

Rejection is also how size is enforced, so these are hard boundaries rather than
truncation points:

| Limit | Value |
|---|---|
| Message length | 4 KiB |
| `structured_fields` total | 32 KiB |
| Nesting depth | bounded |
| Key count | bounded |
| Key length | short strings only |

An event that exceeds any of them is refused whole. Truncating instead would
risk cutting a value mid-secret and defeating the shape matchers.

## Correlation and idempotency

`event_id` is a deterministic UUIDv5. Its preimage contains tenant, mode,
runner, deployment instance, correlation id, and the digest of the **sanitized**
content — computed after redaction, so the identity of an event never depends on
material that was removed.

Two consequences follow. Identical content within one stream is idempotent, so a
retry does not duplicate. And identical content in a different tenant, mode,
runner or instance stream cannot collide in the global dedup table, so one
tenant's events can never be mistaken for another's.

## Delivery

Runtime-log facts share the delivery path with every other fact: the local queue
commits before publish, a PubAck is required before the batch is deleted, and a
crash between the two replays the same `batch_id` — which consumer dedup makes
safe.

A stream that fails to publish blocks later batches from that same stream for
that drain pass. Sequence contiguity is preserved rather than sacrificed for
throughput, because a consumer that sees a gap cannot tell "lost" from "not
yet".

When publishing itself fails, the failure log contains only the structured event
identity and the exception type. The event's own content is never used as
fallback diagnostic output — that would reintroduce exactly the unstructured
path this design exists to avoid.

## What this does not give you

It is not a log aggregation product. There is no query API, no retention policy
you configure here, and no way to request historical events from the runner.

For debugging on the host, read the local JSON on stdout with whatever you
already use. The signed channel answers a different question — not "what
happened" for your eyes, but "what happened, provably" for a consumer that was
not there.
