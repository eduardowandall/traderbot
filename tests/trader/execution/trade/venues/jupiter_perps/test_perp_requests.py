"""Os pedidos da Jupiter Perps (A11a), offline: argumentos, contas e reconcile.

Que o programa aceita os pedidos fica no `tests/live/test_live_perps.py`
(simulação na mainnet); aqui, que a montagem é a esperada e determinística.
Os argumentos são lidos de volta pela IDL, pelo nome de cada campo.
"""

import hashlib
from decimal import Decimal

import pytest
from solders.instruction import AccountMeta
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.execution.market.perps.encode import (
    instruction_accounts,
    instruction_discriminator,
)
from trader.execution.market.perps.idl import _read, instructions
from trader.execution.market.perps.reader import (
    CUSTODIES,
    PERPS_PROGRAM,
    SHORT_COLLATERAL,
    CustodyState,
    JumpRate,
    position_address,
)
from trader.execution.models.perp import PerpTerms
from trader.execution.models.reconcile import (
    MismatchKind,
    market_of,
    reconcile_perps,
)
from trader.execution.trade.venues.jupiter_perps.requests import (
    CLOSE,
    OPEN,
    STOP,
    PerpRequest,
    build_transaction,
    close_request,
    open_request,
    request_address,
    request_counter,
    stop_request,
    token_account,
)
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT

OWNER = Keypair().pubkey()
SHORT = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
LONG = PerpTerms(SOL_MINT, Direction.LONG, Decimal(3))
PRICE = Decimal(100)
SLIP = Decimal("0.02")


def _args(name: str, request: PerpRequest) -> dict:
    """Os argumentos da instrução lidos de volta pela IDL."""
    data = request.instruction.data
    assert data[:8] == instruction_discriminator(name)
    spec = instructions()[name]["args"][0]["type"]
    return _read(spec, data, 8)[0]


def _metas(name: str, request: PerpRequest) -> dict[str, AccountMeta]:
    names = [a["name"] for a in instruction_accounts(name)]
    return dict(zip(names, request.instruction.accounts, strict=True))


def _open(terms=SHORT, minimum=None, key="k") -> PerpRequest:
    return open_request(
        OWNER, terms, 10_000_000, Decimal(30), PRICE, SLIP, key, minimum
    )


def test_discriminators_are_anchors():
    expected = hashlib.sha256(b"global:create_increase_position_market_request")
    assert instruction_discriminator(OPEN) == expected.digest()[:8]
    two = hashlib.sha256(b"global:create_decrease_position_request2").digest()[:8]
    assert instruction_discriminator(STOP) == two


def test_an_open_short_posts_usdc_and_sells_no_lower_than_the_limit():
    request = _open()
    assert _args(OPEN, request) == {
        "sizeUsdDelta": 30_000_000,
        "collateralTokenDelta": 10_000_000,
        "side": "Short",
        "priceSlippage": 98_000_000,  # vende a no mínimo 98
        "jupiterMinimumOut": None,
        "counter": request_counter("k"),
    }
    assert request.instruction.program_id == PERPS_PROGRAM
    assert request.position == position_address(OWNER, SHORT)
    assert request.request == request_address(
        request.position, request_counter("k"), increase=True
    )


def test_accounts_follow_the_idl_order_and_flags():
    metas = _metas(OPEN, _open())
    assert metas["owner"].pubkey == OWNER and metas["owner"].is_signer
    usdc = Pubkey.from_string(SHORT_COLLATERAL)
    assert metas["fundingAccount"].pubkey == token_account(OWNER, usdc)
    assert metas["collateralCustody"].pubkey == CUSTODIES[SHORT_COLLATERAL]
    assert metas["referral"].pubkey == PERPS_PROGRAM  # opcional ausente


def test_a_long_needs_the_swap_minimum_and_pays_up_to_the_limit():
    with pytest.raises(ValueError, match="mínimo da troca"):
        _open(LONG)
    request = _open(LONG, minimum=95_000_000)
    args = _args(OPEN, request)
    assert args["side"] == "Long"
    assert args["priceSlippage"] == 102_000_000  # paga até 102
    assert args["jupiterMinimumOut"] == 95_000_000
    assert _metas(OPEN, request)["collateralCustody"].pubkey == CUSTODIES[SOL_MINT]


def test_the_same_key_targets_the_same_request():
    a = close_request(OWNER, SHORT, PRICE, SLIP, "same")
    b = close_request(OWNER, SHORT, PRICE, SLIP, "same")
    c = close_request(OWNER, SHORT, PRICE, SLIP, "other")
    assert a.request == b.request != c.request


def test_close_takes_the_whole_position_back_in_usdc():
    args = _args(CLOSE, close_request(OWNER, SHORT, PRICE, SLIP, "c"))
    assert args == {
        "collateralUsdDelta": 0,
        "sizeUsdDelta": 0,
        "priceSlippage": 102_000_000,  # recomprar um vendido: até 102
        "jupiterMinimumOut": None,
        "entirePosition": True,
        "counter": request_counter("c"),
    }


@pytest.mark.parametrize(("terms", "above"), [(SHORT, True), (LONG, False)])
def test_the_venue_stop_triggers_against_the_position(terms, above):
    custody = CustodyState(
        mint=SOL_MINT,
        decimals=9,
        owned=1,
        locked=0,
        open_fee_bps=Decimal(6),
        close_fee_bps=Decimal(6),
        jump_rate=JumpRate(Decimal(1), Decimal(2), Decimal(1), Decimal("0.8")),
        oracle=Keypair().pubkey(),
        pyth_oracle=Keypair().pubkey(),
    )
    stop = stop_request(OWNER, terms, Decimal(110), custody, "s")
    args = _args(STOP, stop)
    assert args["requestType"] == "Trigger"
    assert args["triggerPrice"] == 110_000_000
    # um vendido perde subindo: dispara acima; um comprado, abaixo
    assert args["triggerAboveThreshold"] is above
    assert args["entirePosition"] is True and args["priceSlippage"] is None
    metas = _metas(STOP, stop)
    assert metas["custodyDovesPriceAccount"].pubkey == custody.oracle
    assert metas["custodyPythnetPriceAccount"].pubkey == custody.pyth_oracle


def test_the_transaction_is_unsigned_and_capped_by_the_policy():
    tx = build_transaction(OWNER, _open(), priority_fee_lamports=20_000)
    assert tx.message.account_keys[0] == OWNER
    assert all(bytes(s) == bytes(64) for s in tx.signatures)  # nada assinado
    assert len(tx.message.instructions) == 3  # limite, preço, o pedido


def test_reconcile_lists_both_kinds_of_mismatch():
    other = PerpTerms(str(Pubkey.new_unique()), Direction.LONG, Decimal(2))
    found = reconcile_perps(
        map(market_of, [SHORT, LONG]), map(market_of, [LONG, other])
    )
    assert {(m.market_mint, m.direction, m.kind) for m in found} == {
        (SOL_MINT, Direction.SHORT, MismatchKind.MISSING_ON_VENUE),
        (other.market_mint, Direction.LONG, MismatchKind.UNKNOWN_TO_LEDGER),
    }
    # a alavancagem não identifica a posição: o venue não a guarda
    same_side = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(9))
    assert reconcile_perps([market_of(SHORT)], [market_of(same_side)]) == []
