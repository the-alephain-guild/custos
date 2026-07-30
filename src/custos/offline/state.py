"""Durable record of what the offline lane has already applied.

Without this a restarted reconciler starts from generation zero and accepts a
generation it has already passed — at-least-once delivery replays them. It also
gives the lane a real local store to report on, so its readiness claim is
something that was checked rather than something asserted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applied_generation (
    spec_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    container_id TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class AppliedRecord:
    """The generation a spec id reached, and the container the engine named then.

    Only the generation means anything after a restart. The container id records
    an attachment, which belongs to the process that made it, so a reader must not
    take it as evidence that anything is still running.
    """

    generation: int
    container_id: str


class OfflineAppliedStore:
    """Applied generations, keyed by spec id, in one small local file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def load(self) -> dict[str, AppliedRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT spec_id, generation, container_id FROM applied_generation"
            ).fetchall()
        return {row[0]: AppliedRecord(generation=row[1], container_id=row[2]) for row in rows}

    def save(self, spec_id: str, record: AppliedRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO applied_generation (spec_id, generation, container_id) "
                "VALUES (?, ?, ?) ON CONFLICT(spec_id) DO UPDATE SET "
                "generation = excluded.generation, container_id = excluded.container_id",
                (spec_id, record.generation, record.container_id),
            )

    def quick_check(self) -> str:
        """Return SQLite's own verdict on the file, verbatim."""

        with self._connect() as connection:
            return str(connection.execute("PRAGMA quick_check").fetchone()[0])

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None)
