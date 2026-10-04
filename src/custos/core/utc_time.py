"""The one rendering of a UTC instant that enters a signed fact or a digest preimage.

Crucible reads these fields as ``DateTime<Utc>`` and writes them back with chrono's
serde implementation: RFC 3339 with ``Z`` and ``SecondsFormat::AutoSi``. AutoSi writes
no fraction for a whole second, otherwise the shortest of 3, 6 or 9 digits that keeps
every non-zero digit. Rendering the same instant any other way makes the runner's bytes
differ from Crucible's.
"""

from __future__ import annotations

from datetime import UTC, datetime

_NANOS_PER_SECOND = 1_000_000_000


def render_utc(value: datetime) -> str:
    """Render an aware datetime like chrono's RFC 3339 AutoSi serializer."""

    value = value.astimezone(UTC)
    return _render(value.strftime("%Y-%m-%dT%H:%M:%S"), value.microsecond * 1_000)


def render_utc_nanos(epoch_nanoseconds: int) -> str:
    """Render integer nanoseconds since the epoch like chrono's RFC 3339 AutoSi serializer."""

    seconds, nanos = divmod(epoch_nanoseconds, _NANOS_PER_SECOND)
    return _render(datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S"), nanos)


def _render(base: str, nanos: int) -> str:
    if nanos == 0:
        return f"{base}Z"
    if nanos % 1_000_000 == 0:
        return f"{base}.{nanos // 1_000_000:03d}Z"
    if nanos % 1_000 == 0:
        return f"{base}.{nanos // 1_000:06d}Z"
    return f"{base}.{nanos:09d}Z"
