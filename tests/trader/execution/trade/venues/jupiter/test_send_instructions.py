"""`OnChainExecutor.send_instructions` (A11b): o caminho de envio dos perps.

O mesmo dos swaps e do fechamento de conta: programas conferidos, simulação
com a carteira, o envio gravado antes, envio e confirmação.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import (
    USDC,
    confirms,
    inspection_passes,
    signs_instructions,
    simulation,
)
from solders.compute_budget import ID as COMPUTE_BUDGET
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.signature import Signature
from solders.solders import SendTransactionResp

from trader.execution.market.perps.reader import PERPS_PROGRAM as PERPS
from trader.execution.models.intent import SentTx
from trader.execution.trade.venues.jupiter.compute_budget import (
    MAX_UNITS,
    ComputeBudget,
    unit_limit,
    unit_price,
)
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.jupiter.rpc import AsyncRPCClient
from trader.execution.trade.venues.jupiter.tx_inspection import (
    TransactionInspectionError,
)


def _executor():
    rpc = AsyncMock(spec=AsyncRPCClient)
    signs_instructions(rpc)
    inspection_passes(rpc)
    rpc.send_transaction = AsyncMock(
        return_value=SendTransactionResp(value=Signature.new_unique())
    )
    confirms(rpc)
    return OnChainExecutor(Keypair(), rpc, AsyncMock(), 100_000), rpc


def _sent(signed) -> SentTx:
    return SentTx(signed.signature, USDC, "x", 1, 2, signed.last_valid_block_height)


async def test_the_perps_program_needs_the_callers_permission():
    executor, rpc = _executor()
    instruction = Instruction(PERPS, b"", [])
    with pytest.raises(TransactionInspectionError, match="fora da lista"):
        await executor.send_instructions([instruction], _sent, lambda s: None, USDC, 0)
    rpc.send_transaction.assert_not_awaited()


async def test_the_send_is_announced_before_it_happens():
    executor, rpc = _executor()
    order = []
    rpc.send_transaction.side_effect = lambda tx: (
        order.append("send"),
        rpc.send_transaction.return_value,
    )[1]
    signed = await executor.send_instructions(
        [Instruction(PERPS, b"", [])],
        _sent,
        lambda sent: order.append(("announce", sent.signature)),
        USDC,
        0,
        frozenset({str(PERPS)}),
    )
    assert order == [("announce", signed.signature), "send"]


def _budget_of(tx) -> tuple[int, int]:
    """(limite de unidades, preço por unidade) das instruções de orçamento."""
    keys = tx.message.account_keys
    budget = [
        bytes(i.data)
        for i in tx.message.instructions
        if keys[i.program_id_index] == COMPUTE_BUDGET
    ]
    limit = next(int.from_bytes(d[1:5], "little") for d in budget if d[0] == 2)
    price = next(int.from_bytes(d[1:9], "little") for d in budget if d[0] == 3)
    return limit, price


async def test_the_budget_comes_from_the_one_simulation_the_send_makes():
    # A12: assina com o máximo, simula uma vez, assina de novo com o justo
    executor, rpc = _executor()
    rpc.simulate_transaction = AsyncMock(return_value=simulation(units=90_000))
    signed = await executor.send_instructions(
        [Instruction(PERPS, b"", [])],
        _sent,
        lambda sent: None,
        USDC,
        0,
        frozenset({str(PERPS)}),
        ComputeBudget(recent=50_000),
    )
    rpc.simulate_transaction.assert_awaited_once()
    simulated = rpc.simulate_transaction.await_args.args[0]
    assert _budget_of(simulated)[0] == MAX_UNITS
    limit, price = _budget_of(signed.tx)
    assert limit == 117_000  # 90 mil simuladas x 1.3
    assert price * limit <= 100_000 * 10**6  # nunca passa do teto da política
    sent = rpc.send_transaction.await_args.args[0]
    assert str(sent.signatures[0]) == signed.signature  # a reassinada vai


def test_the_unit_price_stays_between_the_floor_and_the_cap():
    cap = 20_000 * 10**6 // 117_000
    assert unit_price(20_000, 117_000, None) == cap  # sem leitura: o teto
    assert unit_price(20_000, 117_000, 10**9) == cap
    assert unit_price(20_000, 117_000, 0) == int(cap * Decimal("0.25"))
    assert unit_limit(None) == MAX_UNITS and unit_limit(10_000) == 100_000


async def test_without_a_budget_only_the_base_fee_is_paid():
    # o fechamento de conta (A15): nada urgente, sem instruções de orçamento
    executor, rpc = _executor()
    signed = await executor.send_instructions(
        [Instruction(PERPS, b"", [])],
        _sent,
        lambda sent: None,
        USDC,
        0,
        frozenset({str(PERPS)}),
    )
    keys = signed.tx.message.account_keys
    programs = {keys[i.program_id_index] for i in signed.tx.message.instructions}
    assert COMPUTE_BUDGET not in programs
