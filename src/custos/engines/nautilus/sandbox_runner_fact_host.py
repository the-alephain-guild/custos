"""RunnerFact-capable adapter for the deterministic sandbox execution host."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from custos.core.runner_fact import (
    SUPPORTED_CURRENCIES,
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactContractError,
)
from custos.core.runner_fact_producer import (
    RunnerFactDeployment,
    VenueLedgerEvidence,
)
from custos.engines.nautilus.host import SandboxSimulationHost


class SandboxRunnerFactHost(SandboxSimulationHost):
    """Expose deterministic sandbox equity through the production fact protocol.

    The underlying simulator remains the execution implementation. This adapter
    owns no business policy: it verifies the signed runner capability binding
    and publishes the exact configured starting equity as an execution fact.
    """

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        capability_receipt: RunnerCapabilityReceipt | None = None,
    ) -> None:
        super().__init__()
        self._tenant_id = tenant_id
        self._capability_receipt = capability_receipt
        self._runner_fact_contexts: dict[str, tuple[RunnerFactDeployment, Decimal]] = {}

    async def deploy(self, spec: dict, credential: dict, artifact: object):
        context = self._build_runner_fact_context(spec)
        engine_receipt = await super().deploy(spec, credential, artifact)
        if context is not None:
            deployment, starting_equity = context
            self._runner_fact_contexts[deployment.deployment_instance_id] = (
                deployment,
                starting_equity,
            )
        return engine_receipt

    async def stop(self, deployment_instance_id: str) -> None:
        await super().stop(deployment_instance_id)
        self._runner_fact_contexts.pop(deployment_instance_id, None)

    def runner_fact_deployments(self) -> tuple[RunnerFactDeployment, ...]:
        return tuple(context[0] for context in self._runner_fact_contexts.values())

    async def runner_fact_risk_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> tuple[Decimal, Sequence[Mapping[str, Any]]]:
        context = self._runner_fact_contexts.get(deployment_instance_id)
        if context is None:
            raise RunnerFactContractError(
                f"unknown sandbox deployment instance {deployment_instance_id!r}"
            )
        deployment, starting_equity = context
        if currency != deployment.currency:
            raise RunnerFactContractError(
                "sandbox risk snapshot currency differs from deployment authority"
            )
        return starting_equity, ()

    async def runner_fact_venue_ledger(
        self,
        deployment_instance_id: str,
        coverage_from: datetime,
        closed_at: datetime,
    ) -> VenueLedgerEvidence:
        del deployment_instance_id, coverage_from, closed_at
        raise RunnerFactContractError("sandbox simulation has no independent venue ledger")

    def _build_runner_fact_context(self, spec: dict) -> tuple[RunnerFactDeployment, Decimal] | None:
        capability = self._capability_receipt
        if capability is None:
            return None
        tenant_id = str(self._tenant_id or "").strip()
        if not tenant_id:
            raise RunnerFactContractError(
                "sandbox RunnerFact publication requires a tenant identity"
            )
        if spec.get("trading_mode") != "sandbox":
            raise RunnerFactContractError("SandboxRunnerFactHost only accepts sandbox deployments")

        deployment_instance_id = str(spec.get("deployment_instance_id") or "").strip()
        deployment_spec_id = str(spec.get("deployment_spec_id") or "").strip()
        deployment_spec_digest = str(spec.get("deployment_spec_digest") or "").strip()
        strategy_id = str(spec.get("strategy_id") or "").strip()
        if not all(
            (
                deployment_instance_id,
                deployment_spec_id,
                deployment_spec_digest,
                strategy_id,
            )
        ):
            raise RunnerFactContractError(
                "sandbox DeploymentSpec lacks canonical RunnerFact identity"
            )

        capability.require_scope_bindings(
            projectors=["settlement", "risk", "health"],
            trading_mode="sandbox",
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=deployment_spec_id,
            deployment_spec_digest=deployment_spec_digest,
            strategy_id=strategy_id,
        )
        authority = RunnerFactAuthority(
            tenant_id=tenant_id,
            trading_mode="sandbox",
            runner_id=capability.runner_id,
            deployment_instance_id=UUID(deployment_instance_id),
            deployment_spec_id=UUID(deployment_spec_id),
            deployment_spec_digest=deployment_spec_digest,
            generation=int(spec["generation"]),
            strategy_id=UUID(strategy_id),
            capability_version_id=capability.capability_version_id,
            capability_version=capability.capability_version,
            capability_manifest_digest=capability.manifest_digest,
        )
        currency = _settlement_currency(spec)
        starting_equity = _starting_equity(spec, currency)
        return (
            RunnerFactDeployment(
                authority=authority,
                deployment_instance_id=deployment_instance_id,
                deployment_spec_id=deployment_spec_id,
                deployment_spec_digest=deployment_spec_digest,
                venue="BINANCE",
                currency=currency,
                reconciliation_available=False,
            ),
            starting_equity,
        )


def _settlement_currency(spec: Mapping[str, Any]) -> str:
    currencies = {
        str(pair).upper().replace("/", "-").split("-")[-1] for pair in spec.get("pairs") or []
    }
    if len(currencies) != 1:
        raise RunnerFactContractError(
            "sandbox RunnerFact v1 requires one settlement currency per deployment"
        )
    currency = next(iter(currencies))
    if currency not in SUPPORTED_CURRENCIES:
        raise RunnerFactContractError(f"settlement currency {currency!r} is outside RunnerFact v1")
    return currency


def _starting_equity(spec: Mapping[str, Any], currency: str) -> Decimal:
    configured = (spec.get("sandbox") or {}).get("starting_balances") or []
    if not configured:
        raise RunnerFactContractError("sandbox RunnerFact requires configured starting_balances")
    total = Decimal("0")
    for raw_balance in configured:
        parts = str(raw_balance).strip().rsplit(maxsplit=1)
        if len(parts) != 2 or parts[1].upper() != currency:
            raise RunnerFactContractError(
                "all sandbox starting balances must use the settlement currency"
            )
        try:
            amount = Decimal(parts[0].replace("_", "").replace(",", ""))
        except InvalidOperation as exc:
            raise RunnerFactContractError(
                "sandbox starting balance amount is not a decimal"
            ) from exc
        if not amount.is_finite() or amount < 0:
            raise RunnerFactContractError(
                "sandbox starting balance must be a finite non-negative decimal"
            )
        total += amount
    return total
