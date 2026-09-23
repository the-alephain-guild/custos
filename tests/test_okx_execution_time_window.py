from datetime import UTC, datetime, timedelta

import pytest

from custos.engines.nautilus.ledger_http import VenueLedgerError
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

END = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
END_MS = int(END.timestamp() * 1000)


class LocalSource(OkxVenueLedgerSource):
    def __init__(self, record_time: int, execution_time: int):
        super().__init__(
            {
                "connector": "okx",
                "trading_mode": "testnet",
                "pairs": ["BTC-USDT"],
                "leverage": 1,
            },
            {key: "local-fixture" for key in ("api_key", "api_secret", "api_passphrase")},
        )
        self.trade = {
            "instId": "BTC-USDT",
            "billId": "10",
            "tradeId": "20",
            "ordId": "30",
            "ts": str(record_time),
            "fillTime": str(execution_time),
            "side": "buy",
            "fillSz": "0.1",
            "fillPx": "100",
            "fee": "-0.01",
            "feeCcy": "USDT",
            "fillPnl": "0",
        }

    def _get(self, path, params, private=True):
        # The complete transport boundary is local; no credential or HTTP request is used.
        if path.endswith("/time"):
            return [{"ts": str(END_MS + 30000)}]
        if path.endswith("/balance"):
            return [{"details": [{"ccy": "USDT", "cashBal": "1000", "availBal": "1000"}]}]
        if path.endswith("/ticker"):
            return [{"instId": "BTC-USDT", "bidPx": "100", "askPx": "100"}]
        if path.endswith("/fills-history"):
            # OKX documents begin/end as filters on ts, the record-generation time.
            return [self.trade] if params["begin"] <= int(self.trade["ts"]) <= params["end"] else []
        raise AssertionError(path)


@pytest.mark.parametrize("record_delay", [0, 1, 3000])
def test_execution_time_keeps_fill_and_commission_in_the_original_window(record_delay):
    executed = END_MS - 1
    source = LocalSource(record_time=executed + record_delay, execution_time=executed)
    previous = source._collect(END - timedelta(seconds=10), END)
    following = source._collect(END, END + timedelta(seconds=10))
    assert len(previous.fills) == len(previous.fees) == 1
    expected = (END - timedelta(milliseconds=1)).isoformat()
    assert previous.fills[0]["occurred_at"] == expected
    assert previous.fees[0]["occurred_at"] == expected
    assert following.fills == following.fees == []


def test_missing_execution_time_cannot_be_reported_as_a_complete_ledger():
    source = LocalSource(record_time=END_MS - 1, execution_time=END_MS - 1)
    del source.trade["fillTime"]
    with pytest.raises(VenueLedgerError):
        source._collect(END - timedelta(seconds=10), END)
