"""
WarmupManager for NautilusTrader strategies.

Manages live-bar buffering during warmup and post-restore checkpoint validation.

State persistence (indicator/global snapshots) moved to the framework
Actor.on_save/on_load path; this manager no longer owns a
SnapshotManager. Snapshot-driven warmup acceleration is applied by
SnapshotCoordinator.apply_loaded_snapshot.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from custos_toolkit.warmup.exceptions import CheckpointValidationError
from custos_toolkit.warmup.snapshot import Checkpoint, PriceSnapshot, WarmupConfig
from nautilus_trader.common import LogColor
from nautilus_trader.model import Bar, InstrumentId

if TYPE_CHECKING:

    from custos_toolkit_nautilus.adapter.pair_context import PairContext


@runtime_checkable
class WarmupStrategyCallbacks(Protocol):
    """
    Protocol defining strategy callbacks needed by WarmupManager.

    Strategies using WarmupManager must implement these methods.
    """

    def get_snapshot_indicators(self) -> dict[str, object]:
        """Return indicators to include in snapshots."""
        ...

    def get_snapshot_state(self) -> dict[str, object]:
        """Return additional state to include in snapshots."""
        ...

    def restore_from_snapshot(self, snapshot: dict[str, object]) -> bool:
        """Restore strategy state from snapshot. Return True if successful."""
        ...


class _IndicatorValue(Protocol):
    @property
    def value(self) -> float: ...


class _Logger(Protocol):
    def debug(self, message: str, *, color: object = ...) -> None: ...
    def error(self, message: str, *, color: object = ...) -> None: ...
    def info(self, message: str, *, color: object = ...) -> None: ...
    def warning(self, message: str, *, color: object = ...) -> None: ...


class WarmupManager:
    """
    Manages live-bar buffering and post-restore checkpoint validation.

    State persistence (Redis/YAML snapshot restore) was removed;
    snapshot restore now happens via the framework on_load path and
    SnapshotCoordinator.apply_loaded_snapshot. This manager keeps bar buffering
    during warmup and the optional checkpoint validation after a snapshot restore.

    Args:
        warmup_config: WarmupConfig or None for no warmup
        strategy_callbacks: Strategy implementing WarmupStrategyCallbacks
        logger: NautilusTrader logger
        strategy_id: Strategy identifier string
        contexts: Optional dict mapping InstrumentId to PairContext (for multi-pair)
    """

    def __init__(
        self,
        warmup_config: WarmupConfig | None,
        strategy_callbacks: WarmupStrategyCallbacks,
        logger: _Logger,
        strategy_id: str,
        contexts: dict["InstrumentId", "PairContext"] | None = None,
    ) -> None:
        self._warmup_config = warmup_config
        self._callbacks = strategy_callbacks
        self._log = logger
        self._strategy_id = strategy_id
        self._contexts = contexts or {}

        # Checkpoint validation state
        self._pending_checkpoints: list[Checkpoint] = []
        self._bars_since_restore: int = 0
        self._checkpoint_validated: bool = False

        # Bar buffering during warmup
        self._buffered_bars: list[Bar] = []
        self._warmup_complete: bool = False

    # CHECKPOINT LOADING (post snapshot-restore)

    def load_pending_checkpoints(self) -> None:
        """Load checkpoints from config for post-restore validation.

        Called by the strategy after a successful framework on_load restore
        (previously triggered inside try_layered_warmup).
        """
        if self._warmup_config is None:
            return

        checkpoints = self._warmup_config.checkpoints
        if checkpoints is None or not checkpoints.points:
            return

        self._pending_checkpoints = list(checkpoints.points)
        self._log.info(f"Loaded {len(self._pending_checkpoints)} checkpoints for validation")

    # CHECKPOINT VALIDATION

    def validate_on_bar(self, bar: Bar, indicators: dict[str, object]) -> None:
        """
        Validate checkpoints on each bar after snapshot restore.

        Should be called from strategy's on_bar() method.

        Args:
            bar: Current bar
            indicators: Dict of indicator name to indicator instance

        Raises:
            CheckpointValidationError: If validation fails
        """
        if not self._pending_checkpoints:
            return

        self._bars_since_restore += 1

        # Find checkpoint matching current offset
        checkpoint = self._find_checkpoint(self._bars_since_restore)
        if checkpoint:
            self._validate_checkpoint(bar, checkpoint, indicators)
            self._pending_checkpoints.remove(checkpoint)

        # All checkpoints validated
        if not self._pending_checkpoints and self._bars_since_restore > 0:
            self._checkpoint_validated = True
            self._log.info(
                "All checkpoints validated successfully",
                color=LogColor.GREEN,
            )

    def _find_checkpoint(self, offset_bars: int) -> Checkpoint | None:
        """Find checkpoint matching the given bar offset."""
        for checkpoint in self._pending_checkpoints:
            if checkpoint.offset_bars == offset_bars:
                return checkpoint
        return None

    def _validate_checkpoint(
        self,
        bar: Bar,
        checkpoint: Checkpoint,
        indicators: dict[str, object],
    ) -> None:
        """
        Validate a single checkpoint against actual indicator values.

        Args:
            bar: Current bar
            checkpoint: Checkpoint to validate
            indicators: Dict of indicator name to indicator instance

        Raises:
            CheckpointValidationError: If validation fails
        """
        warmup_config = self._warmup_config
        assert warmup_config is not None
        config = warmup_config.checkpoints
        assert config is not None

        # 1. Validate bar time
        bar_time = datetime.fromtimestamp(bar.ts_event / 1e9, tz=UTC)
        expected_time = checkpoint.bar_close_time

        if bar_time != expected_time:
            raise CheckpointValidationError(
                f"Bar time mismatch at offset {checkpoint.offset_bars}: "
                f"expected {expected_time.isoformat()}, got {bar_time.isoformat()}"
            )

        # 2. Validate indicator values
        for name, expected in checkpoint.indicators.items():
            indicator = indicators.get(name)
            if indicator is None:
                raise CheckpointValidationError(
                    f"Indicator '{name}' not found for checkpoint validation"
                )

            actual_value = cast(_IndicatorValue, indicator).value
            if expected.value == 0:
                raise CheckpointValidationError(
                    f"Indicator '{name}' checkpoint value is zero (invalid)"
                )

            diff_pct = abs(actual_value - expected.value) / expected.value * 100

            if diff_pct > config.tolerance_pct:
                raise CheckpointValidationError(
                    f"Indicator '{name}' value mismatch at offset {checkpoint.offset_bars}: "
                    f"expected {expected.value}, got {actual_value} "
                    f"(diff: {diff_pct:.3f}% > tolerance {config.tolerance_pct}%)"
                )

            # 3. Validate trend if strict mode enabled
            if config.trend_strict and expected.trend is not None:
                actual_trend = getattr(indicator, "trend", None)
                if actual_trend is not None and actual_trend != expected.trend:
                    raise CheckpointValidationError(
                        f"Indicator '{name}' trend mismatch at offset {checkpoint.offset_bars}: "
                        f"expected {expected.trend}, got {actual_trend}"
                    )

        self._log.info(
            f"Checkpoint {checkpoint.offset_bars} validated OK (bar_time={bar_time.isoformat()})",
            color=LogColor.GREEN,
        )

    # BAR BUFFERING DURING WARMUP

    def buffer_bar(self, bar: Bar) -> None:
        """Buffer a live bar during warmup phase."""
        self._buffered_bars.append(bar)
        self._log.debug(f"Buffered bar: {bar.ts_event} (total: {len(self._buffered_bars)})")

    def get_buffered_bars(self) -> list[Bar]:
        """Get and clear buffered bars after warmup completes."""
        bars = self._buffered_bars.copy()
        self._buffered_bars.clear()
        return bars

    def peek_buffered_bars(self) -> list[Bar]:
        """Get buffered bars without clearing (for multi-pair replay)."""
        return self._buffered_bars.copy()

    def clear_buffered_bars(self) -> None:
        """Clear the buffered bars (call when all pairs have replayed)."""
        self._buffered_bars.clear()

    def mark_warmup_complete(self) -> None:
        """Mark warmup as complete."""
        self._warmup_complete = True

    @property
    def is_warmup_complete(self) -> bool:
        """Check if warmup is complete."""
        return self._warmup_complete

    # HISTORICAL BAR CHECKPOINT VALIDATION

    def validate_on_historical_bar(self, bar: Bar, indicators: dict[str, object]) -> None:
        """
        Validate checkpoints during historical bar replay.

        Uses bar_close_time matching instead of offset counting,
        since historical bars arrive with their original timestamps.

        Args:
            bar: Historical bar being processed
            indicators: Dict of indicator name to indicator instance

        Raises:
            CheckpointValidationError: If validation fails
        """
        bar_time = datetime.fromtimestamp(bar.ts_event / 1e9, tz=UTC)

        # Log historical bar OHLCV for debugging
        self._log.debug(
            f"Historical bar: time={bar_time.isoformat()} "
            f"O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}"
        )

        if not self._pending_checkpoints:
            return

        # Find checkpoint matching this bar's timestamp
        matching_checkpoint = None
        for checkpoint in self._pending_checkpoints:
            if checkpoint.bar_close_time == bar_time:
                matching_checkpoint = checkpoint
                break

        if matching_checkpoint:
            self._validate_checkpoint_values(bar, matching_checkpoint, indicators)
            self._pending_checkpoints.remove(matching_checkpoint)

            if not self._pending_checkpoints:
                self._checkpoint_validated = True
                self._log.info(
                    "All checkpoints validated successfully",
                    color=LogColor.GREEN,
                )

    def _validate_checkpoint_values(
        self,
        bar: Bar,
        checkpoint: Checkpoint,
        indicators: dict[str, object],
    ) -> None:
        """
        Validate indicator values at checkpoint (without time validation).

        Args:
            bar: Current bar
            checkpoint: Checkpoint to validate
            indicators: Dict of indicator name to indicator instance

        Raises:
            CheckpointValidationError: If validation fails
        """
        warmup_config = self._warmup_config
        assert warmup_config is not None
        config = warmup_config.checkpoints
        assert config is not None
        bar_time = datetime.fromtimestamp(bar.ts_event / 1e9, tz=UTC)

        # Validate price if checkpoint contains price data
        if checkpoint.price:
            self._validate_price(bar, checkpoint.price, config.tolerance_pct, bar_time)

        for name, expected in checkpoint.indicators.items():
            indicator = indicators.get(name)
            if indicator is None:
                raise CheckpointValidationError(
                    f"Indicator '{name}' not found for checkpoint validation"
                )

            actual_value = cast(_IndicatorValue, indicator).value
            if expected.value == 0:
                raise CheckpointValidationError(
                    f"Indicator '{name}' checkpoint value is zero (invalid)"
                )

            diff_pct = abs(actual_value - expected.value) / expected.value * 100

            if diff_pct > config.tolerance_pct:
                raise CheckpointValidationError(
                    f"Indicator '{name}' value mismatch: "
                    f"expected {expected.value}, got {actual_value} "
                    f"(diff: {diff_pct:.3f}% > tolerance {config.tolerance_pct}%)"
                )

            # Validate trend if strict mode enabled
            if config.trend_strict and expected.trend is not None:
                actual_trend = getattr(indicator, "trend", None)
                if actual_trend is not None and actual_trend != expected.trend:
                    raise CheckpointValidationError(
                        f"Indicator '{name}' trend mismatch: "
                        f"expected {expected.trend}, got {actual_trend}"
                    )

        self._log.info(
            f"Checkpoint validated OK (bar_time={bar_time.isoformat()})",
            color=LogColor.GREEN,
        )

    def _validate_price(
        self,
        bar: Bar,
        expected: PriceSnapshot,
        tolerance_pct: float,
        bar_time: datetime,
    ) -> None:
        """
        Validate bar OHLCV against checkpoint price snapshot.

        Args:
            bar: Current bar
            expected: Expected price snapshot
            tolerance_pct: Allowed percentage difference
            bar_time: Bar timestamp for error messages

        Raises:
            CheckpointValidationError: If price validation fails
        """
        price_checks = [
            ("open", bar.open.as_double(), expected.open),
            ("high", bar.high.as_double(), expected.high),
            ("low", bar.low.as_double(), expected.low),
            ("close", bar.close.as_double(), expected.close),
        ]

        # Volume validation is optional
        if expected.volume is not None:
            price_checks.append(("volume", bar.volume.as_double(), expected.volume))

        for name, actual, exp in price_checks:
            if exp == 0:
                continue  # Skip zero values
            diff_pct = abs(actual - exp) / exp * 100
            if diff_pct > tolerance_pct:
                raise CheckpointValidationError(
                    f"Price '{name}' mismatch at {bar_time.isoformat()}: "
                    f"expected {exp}, got {actual} "
                    f"(diff: {diff_pct:.3f}% > tolerance {tolerance_pct}%)"
                )

        self._log.debug(f"Price validated: O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
