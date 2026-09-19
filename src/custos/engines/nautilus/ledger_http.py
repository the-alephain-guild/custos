"""Bounded read-only venue transport and exact ledger values."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any


class VenueLedgerError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise VenueLedgerError("venue ledger redirects are forbidden")


class ReadOnlyVenueHttp:
    def __init__(self, base_url: str, minimum_interval: float = 0.45) -> None:
        self.base_url = base_url
        self._last_request = 0.0
        self._minimum_interval = minimum_interval
        self._opener = urllib.request.build_opener(_NoRedirect())

    def get(
        self, path: str, params: dict[str, object], headers: dict[str, str] | None = None
    ) -> Any:
        query = urllib.parse.urlencode(params)
        target = path + ("?" + query if query else "")
        delay = self._minimum_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()
        try:
            request = urllib.request.Request(
                self.base_url + target, headers=headers or {}, method="GET"
            )
            with self._opener.open(request, timeout=15) as response:
                payload = response.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise VenueLedgerError("venue ledger response exceeds the size limit")
            document = json.loads(payload, parse_float=Decimal)
            if (
                not isinstance(document, dict)
                or str(document.get("code")) != "0"
                or "data" not in document
            ):
                raise VenueLedgerError("venue ledger response is not successful")
            return document["data"]
        except VenueLedgerError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise VenueLedgerError("venue ledger request failed") from error


def decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool | float) or value is None:
        raise VenueLedgerError(f"{field} is not exact decimal input")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise VenueLedgerError(f"{field} is not decimal") from error
    if not result.is_finite():
        raise VenueLedgerError(f"{field} is not finite")
    return result


def timestamp_ms(value: object) -> datetime:
    try:
        milliseconds = int(str(value))
    except (ValueError, TypeError) as error:
        raise VenueLedgerError("venue timestamp is invalid") from error
    if milliseconds <= 0:
        raise VenueLedgerError("venue timestamp is not positive")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)


def period_bounds(start: datetime, end: datetime) -> tuple[int, int]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise VenueLedgerError("venue coverage requires an ordered timezone-aware interval")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


def rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise VenueLedgerError("venue rows are malformed")
    return value
