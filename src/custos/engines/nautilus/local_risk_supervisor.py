"""Drive the original cash budget and account-bound Native increase deadline."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeRiskPending
from custos.engines.nautilus.local_risk_observation import PapertradeRiskSource
from custos.local.loss_state import LossBudgetSnapshot, uint
from custos.local.reservations import LocalReservationStore


@runtime_checkable
class ObservationRiskBlocker(Protocol):
    def freeze(self) -> None: ...
    def is_frozen(self) -> bool: ...
    def require_account_observation(self) -> None: ...
    def set_observation_valid_until_ns(self, until_ns: int) -> None: ...
    def set_account_observation_valid_until_ns(self, digest: str, until_ns: int) -> None: ...


class PapertradeRiskSupervisor:
    def __init__(self, *, source: PapertradeRiskSource, poll_interval_ms: int = 250) -> None:
        if type(source) is not PapertradeRiskSource:
            raise RuntimeAuthorityError("risk supervision requires the original concrete source")
        self._source = source
        self._store = source.reservation_store
        blocker = self._store.risk_blocker
        if not isinstance(blocker, ObservationRiskBlocker):
            raise RuntimeAuthorityError("Native blocker lacks account-bound observation support")
        self._blocker = blocker
        self._stopped = self._running = False
        self.audit_unavailable = False
        policy = self._store.authorization.manifest.data_policy
        interval = uint(poll_interval_ms, minimum=1, maximum=1000)
        if interval * 4 > min(policy.max_quote_age_seconds, policy.max_account_age_seconds) * 1000:
            raise RuntimeAuthorityError("risk watchdog interval exceeds the freshness policy")
        self._interval_seconds = interval / 1000
        with self._source.runtime_lease.control_action():
            self._blocker.require_account_observation()
            self._blocker.set_observation_valid_until_ns(0)
            self._budget = self._store.bind_loss_budget()
            self._last_clock_ns = self._store.current_time_ns()
            self._startup_until_ns = self._last_clock_ns + policy.max_account_age_seconds * 10**9

    @property
    def reservation_store(self) -> LocalReservationStore:
        return self._store

    def require_port(self, port: object) -> None:
        if port is not self._source.reduction_port:
            raise RuntimeAuthorityError("risk supervisor names another original execution port")

    def _failure(self) -> None:
        try:
            self._blocker.set_observation_valid_until_ns(0)
        finally:
            self._blocker.freeze()
        try:
            self._budget.freeze_source(reason="storage")
        except RuntimeAuthorityError:
            # The Native guard stays closed when the original owner/audit is unavailable.
            self.audit_unavailable = True

    def refresh(self) -> LossBudgetSnapshot:
        if self._stopped:
            raise RuntimeAuthorityError("risk supervisor has stopped")
        try:
            with self._source.runtime_lease.control_action():
                self._blocker.set_observation_valid_until_ns(0)
                now = self._store.current_time_ns()
                if now < self._last_clock_ns:
                    state = self._budget.freeze_source(reason="clock")
                else:
                    self._last_clock_ns = now
                    sample = self._source.sample(now_ns=now)
                    observation = sample.observation
                    state = self._budget.snapshot()
                    if state.baseline_raw is None and state.state != "latched":
                        if sample.fault:
                            state = self._budget.freeze_source(reason=sample.fault)
                        elif observation.position_ids or observation.unresolved_intent_ids:
                            state = self._budget.initialize(observation)
                        elif observation.cohort_ready:
                            state = self._budget.initialize(observation)
                        elif now >= self._startup_until_ns:
                            state = self._budget.freeze_source(reason="cohort")
                    elif state.baseline_raw is not None:
                        # Preserve a known cash loss before classifying cache/mark faults.
                        state = self._budget.observe(observation)
                        if sample.fault and state.state != "latched":
                            state = self._budget.freeze_source(reason=sample.fault)
                    if (
                        state.state == "ready"
                        and state.ready_until_ns is not None
                        and not self._blocker.is_frozen()
                    ):
                        self._blocker.set_account_observation_valid_until_ns(
                            observation.source_digest, state.ready_until_ns
                        )
                if state.state == "latched":
                    self._blocker.freeze()
                return state
        except Exception:
            self._failure()
            raise

    def require_increase_ready(self) -> None:
        state = self.refresh()
        if state.state == "latched" or self._blocker.is_frozen():
            raise RuntimeAuthorityError("local loss supervision has frozen risk increase")
        if state.state != "ready":
            raise RuntimeRiskPending("local loss supervision awaits a complete observation")

    def close(self) -> None:
        self._stopped = True
        try:
            self._blocker.set_observation_valid_until_ns(0)
        finally:
            self._blocker.freeze()

    async def run(self) -> None:
        if self._running or self._stopped:
            raise RuntimeAuthorityError("risk watchdog cannot be started twice or after stop")
        self._running = True
        try:
            while True:
                self.refresh()
                await asyncio.sleep(self._interval_seconds)
        finally:
            self.close()
            self._running = False
