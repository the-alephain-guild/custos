---
title: "Runtime logs and observability"
sidebar_position: 4
---

Local structured JSON logs go to stdout. On the signed lane, explicitly constructed runtime events can also enter the RunnerFact stream as `RunnerRuntimeLogFact.v1`. The runner does not tail stdout or forward raw exception text to that stream. Offline operation reports local logs and unsigned status instead.

## Runtime-log fact

```json
{
  "kind": "RunnerRuntimeLogFact.v1",
  "event_id": "<deterministic uuidv5>",
  "occurred_at": "<RFC3339 UTC>",
  "level": "INFO",
  "component": "local_cap",
  "message": "...",
  "structured_fields": {},
  "correlation_id": "<uuid>",
  "causation_id": null
}
```

Levels are `DEBUG`, `INFO`, `WARN`, `ERROR`. Component names match `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`. The surrounding signed batch provides tenant, mode, runner, instance, spec/generation, capability, sequence and signature binding.

## Redaction and limits

The redactor recursively anonymizes sensitive field names and recognizable secret values, including tokens, machine credentials, age/PEM keys and assignment-like fragments. If secret material remains recognizable, the entire fact is rejected before SQLite persistence. Unsupported objects, binary floats and non-finite values are also rejected.

Messages are limited to 4 KiB and structured fields to 32 KiB, with bounded nesting, keys and key lengths. Oversized events are rejected rather than truncated. Numeric values use integers or canonical decimal strings.

## Identity and delivery

Runtime-log event UUIDv5 identities include stream authority, correlation id and sanitized-content digest. Matching sanitized events in one stream remain idempotent; different tenant/mode/runner/instance scopes do not share identity.

The durable outbox publishes before recording PubAck completion. Retries retain batch identity and block later batches in a failed stream for that drain pass. Publisher failures use structured identity and exception type; they do not republish the event as raw diagnostic text.

Order lifecycle logs can include initialization, submission, refusal, cancellation and expiry. A strategy signal or submission is not a fill. Correlate by instance and client order id, and inspect execution outcomes separately.

There is no runner log-query API or configurable historical log service. Collect local stdout with your host tooling and retain consumer evidence according to your own operational requirements.
