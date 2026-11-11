"""A host-owned observation guard held through the opening SQLite commit."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, init=False)
class PlatformOpeningObservation:
    _host: object
    token: object
    process_lease: object
    _runtime: object
    _context: object
    _boundary: object

    def __init__(self, *args, **kwargs):
        raise TypeError("opening observation ownership is allocated by the registered host")

    @classmethod
    def _capture(cls, host, authority, token):
        instance = str(authority.deployment_instance_id)
        with host._opening_observation_lock:
            value = object.__new__(cls)
            for field, item in (
                ("_host", host),
                ("token", token),
                ("process_lease", host._platform_process_leases.get(instance)),
                ("_runtime", host._active_nodes.get(instance)),
                ("_context", host._runner_fact_contexts.get(instance)),
                ("_boundary", host._runner_safety_boundaries.get(instance)),
            ):
                object.__setattr__(value, field, item)
            value.assert_current()
            if value._context[0].authority != authority:
                value._retired()
            return value

    @staticmethod
    def _retired():
        from custos.core.runner_fact_producer import RunnerFactObservationRetired

        raise RunnerFactObservationRetired("opening observation ownership is retired")

    def assert_current(self):
        host, lease = self._host, self.process_lease
        if lease is None:
            self._retired()
        instance = str(lease.authority.deployment_instance_id)
        if (
            self._runtime is None
            or self._runtime.task.done()
            or self._context is None
            or self._boundary is None
            or host._active_nodes.get(instance) is not self._runtime
            or host._runner_fact_contexts.get(instance) is not self._context
            or host._runner_safety_boundaries.get(instance) is not self._boundary
            or host._platform_process_leases.get(instance) is not lease
            or host._lifecycle_authorities.get(instance) != lease.authority
            or instance in host._stopping
            or self._boundary.platform_opening_gate is None
            or self._boundary.platform_opening_gate._process_lease is not lease
            or self.token.runtime_identity != id(self._runtime)
            or self.token.observation_epoch != host._opening_observation_epochs.get(instance, 0)
        ):
            self._retired()
        lease.assert_current()

    @property
    def authority(self):
        return self._context[0].authority

    @contextmanager
    def hold(self, authority):
        with self._host._opening_observation_lock, self.process_lease._state_lock:
            if self.process_lease.authority != authority:
                self._retired()
            self.assert_current()
            yield
