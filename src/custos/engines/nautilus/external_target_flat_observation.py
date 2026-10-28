"""Read-only venue snapshot contract for a target flat fence."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class VenueFlatObservation:
    source: str
    connector: str
    pair: str
    observed_at: datetime
    positions_complete: bool
    orders_complete: bool
    open_positions: int
    open_orders: int
