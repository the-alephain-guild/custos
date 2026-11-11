"""A host-owned observation guard held through the opening SQLite commit."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass


class _OpeningObservationEpoch:
    """A native-free retirement fence shared with the host's owner thread."""

    __slots__ = ("lock", "epoch")

    def __init__(self, lock, epoch):
        self.lock = lock
        self.epoch = epoch


@dataclass(frozen=True, slots=True, init=False)
class PlatformOpeningObservation:
    token: object
    process_lease: object
    _authority: object
    _fence: _OpeningObservationEpoch

    def __init__(self, *args, **kwargs):
        raise TypeError("opening observation ownership is allocated by the registered host")

    @classmethod
    def _capture(cls, host, authority, token):
        if threading.get_ident() != host._opening_observation_thread_id:
            cls._retired()
        instance = str(authority.deployment_instance_id)
        with host._opening_observation_lock:
            lease = host._platform_process_leases.get(instance)
            runtime = host._active_nodes.get(instance)
            context = host._runner_fact_contexts.get(instance)
            boundary = host._runner_safety_boundaries.get(instance)
            epoch = host._opening_observation_epochs.get(instance, 0)
            if (
                lease is None
                or runtime is None
                or runtime.task.done()
                or context is None
                or context[0].authority != authority
                or boundary is None
                or host._lifecycle_authorities.get(instance) != lease.authority
                or instance in host._stopping
                or boundary.platform_opening_gate is None
                or boundary.platform_opening_gate._process_lease is not lease
                or token.runtime_identity != id(runtime)
                or token.observation_epoch != epoch
            ):
                cls._retired()
            fence = host._opening_observation_fences.get(instance)
            if fence is None:
                fence = _OpeningObservationEpoch(host._opening_observation_lock, epoch)
                host._opening_observation_fences[instance] = fence
            value = object.__new__(cls)
            for field, item in (
                ("token", token),
                ("process_lease", lease),
                ("_authority", authority),
                ("_fence", fence),
            ):
                object.__setattr__(value, field, item)
            value.assert_current()
            return value

    @staticmethod
    def _retired():
        from custos.core.runner_fact_producer import RunnerFactObservationRetired

        raise RunnerFactObservationRetired("opening observation ownership is retired")

    def assert_current(self):
        with self._fence.lock:
            if self.token.observation_epoch != self._fence.epoch:
                self._retired()
            self.process_lease.assert_current()

    @property
    def authority(self):
        return self._authority

    @contextmanager
    def hold(self, authority):
        with self._fence.lock, self.process_lease._state_lock:
            if self.process_lease.authority != authority:
                self._retired()
            self.assert_current()
            yield
