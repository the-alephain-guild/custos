"""Account inventory valuation without attributing holdings to a strategy."""

from decimal import Decimal

from custos.engines.nautilus.ledger_http import VenueLedgerError, decimal


def book_mid(bid, ask) -> Decimal:
    bid, ask = decimal(bid, "bid price"), decimal(ask, "ask price")
    if bid <= 0 or ask < bid:
        raise VenueLedgerError("cash valuation requires a positive uncrossed book")
    return (bid + ask) / 2


def cash_inventory(balances, currency, prices):
    inventory = []
    seen = set()
    for row in balances:
        asset = row["currency"]
        if asset in seen:
            raise VenueLedgerError("cash inventory repeats an asset")
        seen.add(asset)
        quantity = decimal(row["total"], "cash balance")
        if quantity < 0:
            raise VenueLedgerError("cash inventory cannot include borrowed balances")
        if not quantity and asset != currency:
            continue
        price = Decimal(1) if asset == currency else prices.get(asset)
        if price is None or price <= 0:
            raise VenueLedgerError(f"cash asset has no independent conversion price: {asset}")
        inventory.append({"asset": asset, "quantity": str(quantity), "mark_price": str(price)})
    if currency not in seen:
        inventory.append({"asset": currency, "quantity": "0", "mark_price": "1"})
    for asset, price in prices.items():
        if not any(row["asset"] == asset for row in inventory):
            inventory.append({"asset": asset, "quantity": "0", "mark_price": str(price)})
    return sorted(inventory, key=lambda row: row["asset"])
