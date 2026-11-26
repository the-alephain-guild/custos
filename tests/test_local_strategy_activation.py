"""Local strategy import requires a verified permit, current lease and pinned bytes."""

from datetime import timedelta
from pathlib import Path

import pytest

from custos.artifacts.policy import ArchiveLimitsV1
from custos.engines.nautilus.runtime_loader import (
    NautilusRuntimeEntryPointError,
    NautilusRuntimeEntryPointLoaderV1,
)
from tests.test_local_context_compatibility import release, run_binding  # noqa: F401
from tests.test_local_reservations import local_risk  # noqa: F401


@pytest.fixture
def authorized_run(request, tmp_path):
    material, document, config, toolkit = request.getfixturevalue("run_binding")
    previous = request.getfixturevalue("local_risk")
    previous.lease.retire()
    previous.lease.close()
    owner = previous.make(previous=previous, manifest_override=document)
    kwargs = {
        "material": material,
        "authorization": owner.authorization,
        "lease": owner.lease,
        "effective_config_bytes": config,
        "toolkit": toolkit,
        "activation_parent": (tmp_path / "activate").resolve(),
        "limits": ArchiveLimitsV1(),
        "clock": lambda: owner.clock[0],
    }
    return owner, previous.authorization, kwargs


def test_local_import_uses_a_fresh_verified_snapshot_and_live_lease(authorized_run, monkeypatch):
    owner, _, arguments = authorized_run
    material = arguments["material"]
    package = material.manifest.entry_point.split(".", 1)[0]
    original = material.quarantine.root / package / "runtime.py"
    expected = original.read_bytes()
    original.write_bytes(b"unverified replacement")
    result = object()

    def import_strategy(*, root, module_name, attribute_name, effective_config, execution_context):
        assert root != material.quarantine.root
        assert (root / package / "runtime.py").read_bytes() == expected
        assert module_name == f"{package}.runtime"
        assert attribute_name == "RuntimeAdapterV1"
        assert effective_config["parameters"]["atr_period"] == 10
        assert execution_context.control_source == "local"
        assert execution_context.run_manifest_digest == owner.manifest.digest
        assert execution_context.account_scope == owner.lease.fence.account_scope
        assert execution_context.process_generation == owner.lease.fence.process_generation
        assert execution_context.authorization_epoch == owner.lease.fence.authorization_epoch
        return result

    monkeypatch.setattr(
        NautilusRuntimeEntryPointLoaderV1, "_import_and_build", staticmethod(import_strategy)
    )
    active = NautilusRuntimeEntryPointLoaderV1().load_local(**arguments)
    try:
        assert active.strategy is result
        assert active.quarantine.root.is_dir()
    finally:
        active.quarantine.destroy()


@pytest.mark.parametrize(
    "failure",
    ["lease", "permit", "expired", "expired-during-copy", "lost-store", "config", "import"],
)
def test_activation_refuses_or_cleans_up_without_leaving_unverified_code(
    authorized_run, monkeypatch, failure
):
    owner, previous, arguments = authorized_run
    calls = []

    def import_strategy(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("failed strategy constructor")

    monkeypatch.setattr(
        NautilusRuntimeEntryPointLoaderV1, "_import_and_build", staticmethod(import_strategy)
    )
    if failure == "lease":
        owner.lease.close()
    elif failure == "permit":
        arguments["authorization"] = previous
    elif failure == "expired":
        owner.clock[0] += timedelta(days=10)
    elif failure == "config":
        arguments["effective_config_bytes"] = b"{}"
    elif failure in {"expired-during-copy", "lost-store"}:
        from custos.engines.nautilus import runtime_loader

        original = runtime_loader.quarantine_wheel

        def copy_then_change_owner_evidence(*args, **kwargs):
            isolated = original(*args, **kwargs)
            if failure == "expired-during-copy":
                owner.clock[0] += timedelta(days=10)
            else:
                owner.events.path.unlink()
            return isolated

        monkeypatch.setattr(runtime_loader, "quarantine_wheel", copy_then_change_owner_evidence)

    with pytest.raises(NautilusRuntimeEntryPointError):
        NautilusRuntimeEntryPointLoaderV1().load_local(**arguments)
    assert len(calls) == (1 if failure == "import" else 0)
    parent = Path(arguments["activation_parent"])
    assert not parent.exists() or not list(parent.iterdir())
