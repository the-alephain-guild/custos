"""Closed, immutable payloads for local execution risk audit records."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from custos.core.runtime_authority import RuntimeAuthorityError


def _token(value: str, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
        raise RuntimeAuthorityError(f"{name} must be a bounded string")
    if (not value and not empty) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RuntimeAuthorityError(f"{name} must not contain control characters")


@dataclass(frozen=True)
class RiskMutationRecord:
    operation: str
    request: Mapping[str, str | None]
    outcome: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if self.operation not in {
            "reserve",
            "replace",
            "release",
            "fill",
            "reduce",
            "risk_refused",
            "fact_conflict",
            "cross_run_fact",
            "recovery_required",
        }:
            raise RuntimeAuthorityError("unknown local risk operation")
        fields = {
            "account_scope",
            "run_id",
            "original_run_id",
            "client_order_id",
            "policy_id",
            "requested_notional",
            "new_reserved_notional",
            "reason",
            "fill_identity",
            "position_id",
            "instrument_id",
            "side",
            "fill_notional",
            "fill_quantity",
            "leaves_quantity",
            "reduction_notional",
            "reduction_quantity",
            "reserved_notional",
            "filled_exposure",
            "filled_quantity",
            "state",
            "error_code",
            "source_event_id",
            "input_digest",
        }
        for name in ("request", "outcome"):
            value = dict(getattr(self, name))
            if not value.keys() <= fields:
                raise RuntimeAuthorityError("risk audit fields are not allowlisted")
            for key, item in value.items():
                if item is not None:
                    _token(item, key, empty=True)
            object.__setattr__(self, name, MappingProxyType(value))
        error = self.outcome.get("error_code")
        if "error_code" in self.outcome and (
            type(error) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", error) is None
        ):
            raise RuntimeAuthorityError("risk audit error must be a structured reason code")
        if (
            self.operation
            in {"risk_refused", "fact_conflict", "cross_run_fact", "recovery_required"}
            and error is None
        ):
            raise RuntimeAuthorityError("a refused risk audit requires its reason")

    def payload(self) -> str:
        return json.dumps(
            {
                "operation": self.operation,
                "request": dict(self.request),
                "outcome": dict(self.outcome),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
