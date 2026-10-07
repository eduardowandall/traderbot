"""`OnChainExecutor.send_instructions` (A11b): o caminho de envio dos perps.

O mesmo dos swaps e do fechamento de conta: programas conferidos, simulação
com a carteira, o envio gravado antes, envio e confirmação.
"""

from unittest.mock import AsyncMock

import pytest
from factories import USDC, inspection_passes, signs_instructions
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.signature import Signature
from solders.solders import SendTransactionResp

from trader.execution.market.perps.reader import PERPS_PROGRAM as PERPS
from trader.execution.models.intent import SentTx
from trader.execution.trade.venues.jupiter.async_rpc_client import AsyncRPCClient
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
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
    rpc.check_signature_is_confirmed = AsyncMock(return_value=True)
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
