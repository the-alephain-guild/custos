"""Durable record of what the offline lane has already applied.

Without this the reconciler forgets across a restart and redeploys a generation
it already ran. It also gives the lane a real local store to report on, so its
readiness claim is something that was checked rather than something asserted.
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
