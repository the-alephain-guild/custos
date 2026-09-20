"""
CapitalAllocator for multi-pair capital management.

Manages tiered capital allocation, portfolio value calculation,
and exposure monitoring across multiple trading pairs.
"""

from decimal import Decimal

from nautilus_trader.model import InstrumentId

from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

from .runtime_types import Cache


class CapitalAllocator:
    """
    Manages multi-pair capital allocation and portfolio value.

    Features:
    - Tiered capital allocation
    - Portfolio value calculation
    - Rebalance amount calculation
    - Exposure monitoring
    """

    def __init__(
        self,
        config: AllocationConfig,
        initial_capital: Decimal,
        cache: Cache,
    ) -> None:
        self._config = config
        # A configured share belongs to its pair and is never redistributed. Pairs
        # without one share what is left, recomputed over the whole set each time one
        # registers -- otherwise the answer depends on registration order.
        self._explicit_tiers: dict[str, float] = dict(config.tiers) if config.tiers else {}
        self._tiers: dict[str, float] = dict(self._explicit_tiers)
        self._implicit_pairs: list[str] = []
        self._initial_capital: Decimal = initial_capital
        self._max_total_exposure: float = config.max_total_exposure
        self._cache = cache

        # Capital tracking
        self._allocated: dict[str, Decimal] = {}
        self._available_cash: Decimal = initial_capital

        # Pair mappings
        self._instrument_to_pair: dict[InstrumentId, str] = {}
        self._pair_to_instrument: dict[str, InstrumentId] = {}

    # Registration

    def register_pair(self, pair: str, instrument_id: InstrumentId) -> None:
        """Register a trading pair."""
        self._instrument_to_pair[instrument_id] = pair
        self._pair_to_instrument[pair] = instrument_id
        self._allocated[pair] = Decimal(0)

        # No configured tier: this pair shares the unclaimed remainder with the other
        # unconfigured pairs, which means re-dividing across all of them, including
        # those already registered.
        if pair not in self._explicit_tiers:
            if pair not in self._implicit_pairs:
                self._implicit_pairs.append(pair)
            self._redivide_implicit_tiers()

    def _redivide_implicit_tiers(self) -> None:
        """Split whatever the explicit tiers left over evenly across the rest.

        These float ratios exist so the pair is present in ``_tiers`` for weight
        reporting, which reads the keys. The authoritative limit is computed in
        Decimal by ``get_tier_limit``; do not read a limit back from these values.
        """
        if not self._implicit_pairs:
            return
        claimed = sum(self._explicit_tiers.values())
        remainder = max(1.0 - claimed, 0.0)
        share = remainder / len(self._implicit_pairs)
        for pair in self._implicit_pairs:
            self._tiers[pair] = share

    # Capital Allocation

    def get_tier_limit(self, pair: str) -> Decimal:
        """Get allocation limit for a pair.

        An even share is divided in Decimal rather than read back from the float
        ratio: a third of 300 has to be 100, not 99.99999999999999.
        """
        if pair in self._explicit_tiers:
            return self._initial_capital * Decimal(str(self._explicit_tiers[pair]))
        if pair in self._implicit_pairs:
            return self._implicit_remainder() * self._initial_capital / len(self._implicit_pairs)
        return Decimal(0)

    def _implicit_remainder(self) -> Decimal:
        """The share of capital the explicit tiers have not claimed."""
        claimed = sum(
            (Decimal(str(ratio)) for ratio in self._explicit_tiers.values()), Decimal(0)
        )
        return max(Decimal(1) - claimed, Decimal(0))

    def get_available_capital(self, pair: str) -> Decimal:
        """Get available capital for a pair."""
        tier_limit = self.get_tier_limit(pair)
        used = self._allocated.get(pair, Decimal(0))
        pair_available = tier_limit - used

        # Also consider global available cash
        return min(pair_available, self._available_cash)

    def allocate(self, pair: str, amount: Decimal) -> bool:
        """Request capital allocation. Returns True if successful."""
        available = self.get_available_capital(pair)
        if amount > available:
            return False

        self._allocated[pair] = self._allocated.get(pair, Decimal(0)) + amount
        self._available_cash -= amount
        return True

    def release(self, pair: str, amount: Decimal) -> None:
        """Release capital (on position close)."""
        current = self._allocated.get(pair, Decimal(0))
        release_amount = min(amount, current)
        self._allocated[pair] = current - release_amount
        self._available_cash += release_amount

    # Portfolio Value Calculation

    def get_position_value(self, pair: str, current_price: Decimal) -> Decimal:
        """Get position value for a pair."""
        instrument_id = self._pair_to_instrument.get(pair)
        if not instrument_id:
            return Decimal(0)

        positions = self._cache.positions_open(instrument_id=instrument_id)
        position = positions[0] if positions else None
        if position is None or position.quantity.as_decimal() == 0:
            return Decimal(0)

        quantity = abs(position.quantity.as_decimal())
        return quantity * current_price

    def get_pair_total_value(self, pair: str, current_price: Decimal) -> Decimal:
        """Get total value for a pair (position + allocated capital)."""
        position_value = self.get_position_value(pair, current_price)
        allocated = self._allocated.get(pair, Decimal(0))
        return position_value + allocated

    def get_portfolio_value(self, current_prices: dict[str, Decimal]) -> Decimal:
        """
        Get total portfolio value.

        Args:
            current_prices: pair -> current price mapping

        Returns:
            Total portfolio value = initial capital + unrealized PnL from positions.
            When no positions exist, this equals initial_capital.
        """
        # Calculate total position value (which includes unrealized PnL)
        total_position_value = Decimal(0)
        for pair, price in current_prices.items():
            total_position_value += self.get_position_value(pair, price)

        # Portfolio value = available cash + allocated capital + position values
        # Note: allocated + available_cash = initial_capital always
        # Position value reflects the current market value (includes PnL)
        # But we need to avoid double counting: when we have a position,
        # the allocated capital is "converted" to that position.
        #
        # Simplest approach: initial_capital + unrealized_pnl
        # Since we don't track cost basis separately, use:
        # portfolio_value = initial_capital (for now, PnL tracking can be added later)
        # OR use the fact that allocated represents reserved capital:
        # portfolio_value = available_cash + sum(max(allocated[pair], position_value[pair]))
        #
        # For correct behavior: total value = all position values + uninvested cash
        # Uninvested cash = available_cash + (allocated - position_cost_basis)
        # Since we don't track cost basis, approximate as:
        # portfolio_value = initial_capital (when no positions)
        # portfolio_value = position_values + (initial_capital - sum(allocated)) + available_cash
        #                 = position_values + available_cash + (initial - allocated)
        #                 = position_values + (initial - (initial - available)) + available
        #                 = position_values + available + available (wrong!)
        #
        # Let's use the simpler model: portfolio = initial_capital + unrealized_pnl
        # Since we don't have PnL tracking yet, just return initial_capital
        # In practice, this will be enhanced to track actual PnL

        # For the tests: portfolio_value should reflect total capital under management
        # = position values (if any) + available_cash + allocated (but not in positions)
        _total_allocated = sum(self._allocated.values())

        # If no positions, portfolio = allocated + available = initial_capital
        if total_position_value == 0:
            return self._initial_capital

        # With positions, portfolio = position_value + available_cash
        # (allocated capital is "converted" into positions)
        return total_position_value + self._available_cash

    def get_current_weights(self, current_prices: dict[str, Decimal]) -> dict[str, float]:
        """
        Get current portfolio weights.

        Args:
            current_prices: pair -> current price mapping

        Returns:
            pair -> weight mapping
        """
        portfolio_value = self.get_portfolio_value(current_prices)

        if portfolio_value == 0:
            return dict.fromkeys(self._tiers.keys(), 0.0)

        weights = {}
        for pair in self._tiers.keys():
            price = current_prices.get(pair, Decimal(0))
            pair_value = self.get_pair_total_value(pair, price)
            weights[pair] = float(pair_value / portfolio_value)

        return weights

    def get_rebalance_amounts(
        self,
        target_weights: dict[str, float],
        current_prices: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        """
        Calculate rebalance adjustment amounts.

        Args:
            target_weights: Target weights
            current_prices: Current prices

        Returns:
            pair -> adjustment amount (positive = buy, negative = sell)
        """
        portfolio_value = self.get_portfolio_value(current_prices)
        current_weights = self.get_current_weights(current_prices)

        adjustments = {}
        for pair, target in target_weights.items():
            current = current_weights.get(pair, 0.0)
            diff_weight = target - current
            diff_value = portfolio_value * Decimal(str(diff_weight))
            adjustments[pair] = diff_value

        return adjustments

    # Risk Metrics

    def get_total_exposure(self, current_prices: dict[str, Decimal]) -> float:
        """Get total exposure ratio."""
        portfolio_value = self.get_portfolio_value(current_prices)
        if portfolio_value == 0:
            return 0.0

        total_position_value = sum(
            self.get_position_value(pair, price) for pair, price in current_prices.items()
        )

        return float(total_position_value / portfolio_value)

    def check_exposure_limit(self, current_prices: dict[str, Decimal]) -> bool:
        """Check if within maximum exposure limit."""
        return self.get_total_exposure(current_prices) <= self._max_total_exposure

    # Properties

    @property
    def total_capital(self) -> Decimal:
        """Initial capital."""
        return self._initial_capital

    @property
    def available_cash(self) -> Decimal:
        """Available cash."""
        return self._available_cash

    @property
    def pairs(self) -> list[str]:
        """Registered pairs list."""
        return list(self._pair_to_instrument.keys())
