import pytest

from custos.core.runtime_authority import RuntimeAccountIdentity, RuntimeAuthorityError

EXCHANGE = "0x6cd5661646289fb6e65ea5c032310fded797d0a2"
WALLET = "0x1111111111111111111111111111111111111111"


def account(**overrides):
    values = {
        "wallet_address": WALLET,
        "adapter_chain_id": 999,
        "adapter_exchange": EXCHANGE,
        "declared_chain_id": 999,
        "declared_exchange": EXCHANGE,
        "trading_mode": "live",
    }
    values.update(overrides)
    return RuntimeAccountIdentity.from_papertrade_adapter(**values)


def test_one_wallet_has_one_scope_independent_of_address_case():
    first = account()
    same = account(adapter_exchange="0x" + EXCHANGE[2:].upper())
    assert first == same
    assert first.scope_digest == same.scope_digest
    assert len(first.scope_digest) == 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"declared_chain_id": 1},
        {"adapter_chain_id": 1, "declared_chain_id": 1},
        {"declared_exchange": WALLET},
        {"adapter_exchange": WALLET, "declared_exchange": WALLET},
        {"adapter_chain_id": 999.0, "declared_chain_id": 999.0},
        {"declared_chain_id": 999.0},
        {"trading_mode": "testnet"},
    ],
)
def test_false_adapter_domains_cannot_split_a_real_wallet_scope(overrides):
    with pytest.raises(RuntimeAuthorityError):
        account(**overrides)
