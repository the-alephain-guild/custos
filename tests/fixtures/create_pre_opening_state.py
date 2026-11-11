"""Run with the committed predecessor source on PYTHONPATH, never the current source."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from custos.core.platform_process_fence import PlatformProcessRegistry
from custos.core.runner_fact import heartbeat
from tests.test_platform_process_fence import signed_sandbox_account
from tests.test_runner_fact_store import _identity, _runner_authority


async def main():
    output = Path(sys.argv[1])
    import custos.core.runner_fact as predecessor_module

    if not Path(predecessor_module.__file__).resolve().is_relative_to(Path.cwd()):
        raise RuntimeError("the predecessor fixture imported a different source tree")
    store, account = await signed_sandbox_account(output / "runner.sqlite3")
    desired = await store.load_durable_desired_command(
        account.context.authority.deployment_instance_id
    )
    authority = replace(_runner_authority(desired), strategy_id=desired.command.strategy_id)
    await store._outbox.enqueue(
        authority,
        _identity(),
        [heartbeat(event_id=uuid4(), status="degraded", observed_at="2026-11-04T00:00:00Z")],
    )
    registry = PlatformProcessRegistry.initialize(output / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    lease.close()
    snapshot = store.platform_storage_snapshot()
    metadata = {
        "source_revision": "bbfa18ea41d5603641b10c7129f21ff9b28b1d50",
        "store_id": str(snapshot.store_id),
        "mutation_seq": snapshot.mutation_seq,
        "registry_id": str(registry.binding.registry_id),
        "host_scope_id": str(registry.binding.host_scope_id),
        "instance_id": str(account.context.authority.deployment_instance_id),
    }
    registry.close()
    with sqlite3.connect(output / "runner.sqlite3") as connection:
        metadata["outbox"] = [
            base64.b64encode(row[0]).decode()
            for row in connection.execute(
                "SELECT payload FROM runner_fact_outbox ORDER BY source_seq_start"
            )
        ]
        metadata["stream"] = connection.execute("SELECT * FROM runner_fact_stream").fetchall()
        metadata["bindings"] = connection.execute("SELECT * FROM platform_process_heads").fetchall()
        objects = [
            {"type": row[0], "name": row[1], "table": row[2], "sql": " ".join(row[3].split())}
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
            )
        ]
    (output / "expected.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "schema.json").write_text(
        json.dumps({"source_revision": metadata["source_revision"], "objects": objects}, indent=2)
        + "\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
