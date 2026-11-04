"""The one rendering of a UTC instant that enters a signed fact or a digest preimage.

Crucible reads these fields as ``DateTime<Utc>`` and writes them back with chrono's
serde implementation: RFC 3339 with ``Z`` and ``SecondsFormat::AutoSi``. AutoSi writes
no fraction for a whole second, otherwise the shortest of 3, 6 or 9 digits that keeps
every non-zero digit. Rendering the same instant any other way makes the runner's bytes
differ from Crucible's.
"""

from __future__ import annotations

import re
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


def normalize_utc_timestamp(value: datetime | str) -> str:
    """Normalize RFC3339 while retaining all nine source fractional digits."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        current = value.astimezone(UTC)
        base = f"{current.year:04d}-{current.month:02d}-{current.day:02d}T{current.hour:02d}:{current.minute:02d}:{current.second:02d}"
        return _render(base, current.microsecond * 1_000)
    if not isinstance(value, str):
        raise ValueError("timestamp must be a datetime or RFC3339 string")
    match = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    )
    if match is None:
        raise ValueError("timestamp must be RFC3339 with at most nine fractional digits")
    base, fraction, offset = match.groups()
    parsed = datetime.fromisoformat(base + ("+00:00" if offset == "Z" else offset))
    current = parsed.astimezone(UTC)
    utc_base = f"{current.year:04d}-{current.month:02d}-{current.day:02d}T{current.hour:02d}:{current.minute:02d}:{current.second:02d}"
    nanos = int((fraction or "0").ljust(9, "0"))
    return _render(utc_base, nanos)


def utc_timestamp_key(value: str) -> tuple[str, int]:
    """Order UTC instants without discarding sub-microsecond source precision."""
    canonical = normalize_utc_timestamp(value)
    base, _, fraction = canonical[:-1].partition(".")
    return base, int(fraction.ljust(9, "0"))


def _render(base: str, nanos: int) -> str:
    if nanos == 0:
        return f"{base}Z"
    if nanos % 1_000_000 == 0:
        return f"{base}.{nanos // 1_000_000:03d}Z"
    if nanos % 1_000 == 0:
        return f"{base}.{nanos // 1_000:06d}Z"
    return f"{base}.{nanos:09d}Z"
