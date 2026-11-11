"""Atomic extension of the exact registered pre-opening V1 store, without data replacement."""

from __future__ import annotations

import json
from importlib.resources import files
from uuid import UUID

from custos.core.platform_process_identity import (
    PLATFORM_STATE_TABLES,
    PlatformFenceError,
    platform_counter_triggers,
)


def _objects(connection):
    return [
        {"type": row[0], "name": row[1], "table": row[2], "sql": " ".join(row[3].split())}
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
        )
    ]


def upgrade_known_opening_state(connection, validate_current):
    predecessor = json.loads(
        files("custos.core").joinpath("_runner_state_pre_opening_v1.json").read_text()
    )
    previous_tables = {row["name"] for row in predecessor["objects"] if row["type"] == "table"}
    current_tables = PLATFORM_STATE_TABLES | {"runner_store_identity"}
    actual_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if actual_tables == current_tables:
        return
    if actual_tables != previous_tables:
        raise PlatformFenceError("platform_storage_schema_differs")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _objects(connection) != predecessor["objects"]:
            raise PlatformFenceError("platform_storage_schema_differs")
        header = connection.execute(
            "SELECT singleton,schema_version FROM runner_state_schema"
        ).fetchall()
        if len(header) != 1 or tuple(header[0]) != (1, 1):
            raise PlatformFenceError("platform_storage_schema_differs")
        identity = connection.execute(
            "SELECT store_id,schema_revision,mutation_seq FROM runner_store_identity"
        ).fetchall()
        try:
            identifier = UUID(identity[0][0]) if len(identity) == 1 else None
        except (ValueError, TypeError, AttributeError) as error:
            raise PlatformFenceError("platform_storage_identity_invalid") from error
        if (
            identifier is None
            or identifier.int == 0
            or str(identifier) != identity[0][0]
            or identity[0][1] != 1
            or type(identity[0][2]) is not int
            or not 0 <= identity[0][2] < 2**63
        ):
            raise PlatformFenceError("platform_storage_identity_invalid")
        from custos.core.opening_release_state import install

        install(connection)
        present = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for name, statement in platform_counter_triggers().items():
            if name not in present:
                connection.execute(statement)
        validate_current(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
