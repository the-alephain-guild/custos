"""Distinct risk sources and complete signing plans for an opening observation."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal

from custos.core.utc_time import normalize_utc_timestamp

if TYPE_CHECKING:
    from custos.core.runner_fact import RunnerFactAuthority
    from custos.core.runner_fact_producer import RunnerFactObservationToken


@dataclass(frozen=True, slots=True, init=False)
class IndependentRiskSample:
    equity: Decimal
    currency: str
    _positions_json: bytes
    observed_at: str
    sample_id: str
    source: Literal["portfolio", "simulation"]

    def __init__(self, equity, currency, positions, observed_at, sample_id, source):
        from custos.core.runner_fact import _canonical_json_bytes

        for field, value in (
            ("equity", equity),
            ("currency", currency),
            ("observed_at", observed_at),
            ("sample_id", sample_id),
            ("source", source),
            ("_positions_json", _canonical_json_bytes(list(positions))),
        ):
            object.__setattr__(self, field, value)
        self.__post_init__()

    @property
    def positions(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(json.loads(self._positions_json))

    def __post_init__(self) -> None:
        from custos.core.runner_fact import RunnerFactContractError, _currency, _signed_decimal

        if not isinstance(self.equity, Decimal) or not self.equity.is_finite():
            raise RunnerFactContractError("independent risk requires an exact finite amount")
        _signed_decimal(self.equity, "risk equity", bounded=True)
        _currency(self.currency)
        if (
            self.source not in {"portfolio", "simulation"}
            or not self.sample_id
            or normalize_utc_timestamp(self.observed_at) != self.observed_at
        ):
            raise RunnerFactContractError("independent risk source or time is invalid")


@dataclass(frozen=True, slots=True)
class RiskSampleUnavailable:
    observed_at: str
    reason: str
    source: Literal["portfolio", "simulation"] = "portfolio"


@dataclass(frozen=True, slots=True, init=False)
class AdmittedNativeCashSample:
    equity: Decimal
    currency: str
    observed_at: str
    sample_id: str
    _inventory_digest: str

    @property
    def positions(self) -> tuple[Mapping[str, Any], ...]:
        return ()

    @property
    def source(self) -> Literal["native_cash"]:
        return "native_cash"

    @classmethod
    def from_inventory(
        cls,
        inventory: Mapping[str, Any],
        authority: RunnerFactAuthority,
        currency: str,
        manifest: Mapping[str, Any],
    ) -> AdmittedNativeCashSample:
        from custos.core.opening_inventory_contract import (
            OpeningInventory,
            normalize_opening_inventory,
            require_opening_inventory_support,
        )
        from custos.core.runner_fact import RunnerFactContractError, _canonical_json_bytes

        parsed = OpeningInventory.model_validate(inventory)
        binding = parsed.equity_binding
        if (
            not parsed.inventory_complete
            or binding.status != "coherent"
            or parsed.scope.source.kind != "sandbox_engine"
            or parsed.observations.ordinary_orders.rows
        ):
            raise RunnerFactContractError("native cash sample has no complete coherent capture")
        normalized = normalize_opening_inventory(
            inventory,
            authority=authority,
            amount=binding.amount,
            currency=currency,
            observed_at=binding.observed_at,
        )
        require_opening_inventory_support(normalized, manifest)
        sample = object.__new__(cls)
        for field, value in (
            ("equity", Decimal(binding.amount)),
            ("currency", currency),
            ("observed_at", binding.observed_at),
            ("sample_id", binding.capture_token),
            ("_inventory_digest", sha256(_canonical_json_bytes(normalized)).hexdigest()),
        ):
            object.__setattr__(sample, field, value)
        return sample


@dataclass(frozen=True, slots=True, init=False)
class OpeningObservationBundle:
    authority: RunnerFactAuthority
    token: RunnerFactObservationToken
    independent: IndependentRiskSample | RiskSampleUnavailable
    trading_gate: Literal["awaiting_release", "released"]
    _inventory_json: bytes
    native: AdmittedNativeCashSample | None
    currency: str

    def __init__(self, authority, token, independent, trading_gate, inventory, native, currency):
        from custos.core.runner_fact import _canonical_json_bytes

        for field, value in (
            ("authority", authority),
            ("token", token),
            ("independent", independent),
            ("trading_gate", trading_gate),
            ("native", native),
            ("currency", currency),
            ("_inventory_json", _canonical_json_bytes(inventory)),
        ):
            object.__setattr__(self, field, value)
        self.__post_init__()

    @property
    def inventory(self) -> Mapping[str, Any]:
        # The validated bytes are immutable; each reader gets its own mutable DTO.
        return json.loads(self._inventory_json)

    def __post_init__(self) -> None:
        from custos.core.opening_inventory_contract import normalize_opening_inventory
        from custos.core.runner_fact import RunnerFactContractError, _signed_decimal

        sample = self.native or self.independent
        binding = self.inventory.get("equity_binding", {})
        amount = (
            _signed_decimal(sample.equity, "opening observation equity", bounded=True)
            if not isinstance(sample, RiskSampleUnavailable)
            else "0"
        )
        currency = (
            sample.currency if not isinstance(sample, RiskSampleUnavailable) else self.currency
        )
        if any(
            isinstance(risk, IndependentRiskSample) and risk.currency != self.currency
            for risk in (self.independent,)
        ) or (self.native is not None and self.native.currency != self.currency):
            raise RunnerFactContractError("opening risk sources differ from the expected currency")
        normalize_opening_inventory(
            self.inventory,
            authority=self.authority,
            amount=amount,
            currency=currency,
            observed_at=sample.observed_at,
        )
        if self.native is not None and (
            binding.get("status") != "coherent"
            or binding.get("capture_token") != self.native.sample_id
            or sha256(self._inventory_json).hexdigest() != self.native._inventory_digest
        ):
            raise RunnerFactContractError("native risk sample differs from its opening inventory")
        if self.native is None and binding.get("status") == "coherent":
            raise RunnerFactContractError("coherent inventory has no admitted native risk sample")
        if self.trading_gate not in {"awaiting_release", "released"}:
            raise RunnerFactContractError("opening observation gate is invalid")


async def collect_opening_observation(
    host,
    *,
    authority: RunnerFactAuthority,
    connector: str,
    currency: str,
    manifest: Mapping[str, Any],
    source: Literal["portfolio", "simulation"] = "portfolio",
) -> OpeningObservationBundle:
    from custos.core.opening_inventory_contract import (
        normalize_opening_inventory,
        require_opening_inventory_support,
        unavailable_opening_inventory,
    )
    from custos.core.runner_fact import _canonical_json_bytes, _signed_decimal
    from custos.core.runner_fact_producer import RunnerFactObservationRetired
    from custos.core.utc_time import render_utc_nanos

    manifest = json.loads(_canonical_json_bytes(manifest))
    instance = str(authority.deployment_instance_id)
    token = await host.runner_fact_observation_token(authority)
    try:
        equity, positions = await host.runner_fact_risk_snapshot(instance, currency)
        at = render_utc_nanos(time.time_ns())
        sample_id = sha256(
            _canonical_json_bytes(
                {
                    "source": source,
                    "token": repr(token),
                    "observed_at": at,
                    "equity": _signed_decimal(equity, "risk equity", bounded=True),
                    "currency": currency,
                    "positions": list(positions),
                }
            )
        ).hexdigest()
        independent = IndependentRiskSample(
            Decimal(_signed_decimal(equity, "risk equity", bounded=True)),
            currency,
            tuple(dict(row) for row in positions),
            at,
            sample_id,
            source,
        )
    except RunnerFactObservationRetired:
        raise
    except Exception:
        independent = RiskSampleUnavailable(
            render_utc_nanos(time.time_ns()), "portfolio_unavailable", source
        )
    native = None
    try:
        gate, inventory = await host.runner_fact_opening_snapshot(instance)
        inventory = json.loads(_canonical_json_bytes(inventory))
        expected_scope = {
            field: str(getattr(authority, field))
            if field in {"runner_id", "deployment_instance_id", "deployment_spec_id"}
            else getattr(authority, field)
            for field in (
                "tenant_id",
                "trading_mode",
                "runner_id",
                "deployment_instance_id",
                "deployment_spec_id",
                "deployment_spec_digest",
                "generation",
            )
        }
        if isinstance(inventory.get("scope"), Mapping) and any(
            field in inventory["scope"] and inventory["scope"][field] != value
            for field, value in expected_scope.items()
        ):
            raise RunnerFactObservationRetired("opening inventory scope is retired")
        if inventory["scope"].get("connector") != connector:
            raise RunnerFactObservationRetired("opening inventory connector is retired")
        for field in ("capability_version_id", "capability_version", "capability_manifest_digest"):
            expected = getattr(authority, field)
            if inventory["capability_binding"].get(field) != (
                str(expected) if field == "capability_version_id" else expected
            ):
                raise RunnerFactObservationRetired("opening inventory capability is retired")
        binding = inventory["equity_binding"]
        if binding["status"] == "coherent":
            native = AdmittedNativeCashSample.from_inventory(
                inventory, authority, currency, manifest
            )
        amount = (
            _signed_decimal(native.equity, "native opening equity", bounded=True)
            if native is not None
            else (
                _signed_decimal(independent.equity, "independent opening equity", bounded=True)
                if isinstance(independent, IndependentRiskSample)
                else "0"
            )
        )
        inventory = normalize_opening_inventory(
            inventory,
            authority=authority,
            amount=amount,
            currency=currency,
            observed_at=native.observed_at if native is not None else independent.observed_at,
        )
        require_opening_inventory_support(inventory, manifest)
    except RunnerFactObservationRetired:
        raise
    except Exception:
        gate = "awaiting_release"
        native = None
        inventory = unavailable_opening_inventory(authority, connector, "source_unavailable")
    if await host.runner_fact_observation_token(authority) != token:
        raise RunnerFactObservationRetired("opening bundle retired during its source reads")
    return OpeningObservationBundle(
        authority, token, independent, gate, inventory, native, currency
    )


@dataclass(frozen=True, slots=True, init=False)
class ObservabilityFactPlan:
    """Both alternatives retain immutable bytes of their complete source plans."""

    _primary_json: bytes
    _fallback_json: bytes | None

    def __init__(self, primary, fallback):
        from custos.core.runner_fact import _canonical_json_bytes

        object.__setattr__(self, "_primary_json", _canonical_json_bytes(list(primary)))
        object.__setattr__(
            self,
            "_fallback_json",
            None if fallback is None else _canonical_json_bytes(list(fallback)),
        )

    @property
    def primary(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(json.loads(self._primary_json))

    @property
    def fallback(self) -> tuple[Mapping[str, Any], ...] | None:
        return None if self._fallback_json is None else tuple(json.loads(self._fallback_json))

    @classmethod
    def from_facts(
        cls,
        primary: Sequence[Mapping[str, Any]],
        fallback: Sequence[Mapping[str, Any]] | None,
    ) -> ObservabilityFactPlan:
        return cls(primary, fallback)
