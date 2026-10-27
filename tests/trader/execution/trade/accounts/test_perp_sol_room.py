"""A26: uma perp só abre com o SOL que os pedidos dela travam (G4).

A abertura gasta USDC, mas cada pedido da Jupiter trava rent em SOL até o
keeper rodar, o stop trava o mesmo enquanto a posição está aberta, e a
primeira abertura de um mercado e lado paga a conta da posição (A25). Sem
esse SOL a posição abriria e o stop ou o fechamento falhariam na rede: a
compra é recusada antes da intenção. O replay não confere (não tem SOL).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import PerpOracle

from trader.execution.models.perp import (
    PERP_POSITION_RENT_LAMPORTS,
    PERP_REQUEST_LOCK_LAMPORTS,
    PerpTerms,
    open_sol_lamports,
)
from trader.execution.trade.accounts.perp import SolRoomError
from trader.execution.trade.accounts.spot import WalletShortfallError
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger import Ledger
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.paper.perps import SimulatedPerpsVenue
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
T0 = datetime(2026, 10, 13, 12, tzinfo=UTC)
SHORT3 = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
CAP = 100_000  # o `max_priority_fee_lamports` padrão
SEND = 5000 + CAP
POLICY = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=1000,
    max_trades_per_hour_per_bucket=1000,
    perps_enabled=True,
)


class _Paper:
    def __init__(self, tmp_path, sol: str):
        self.tmp_path = tmp_path
        self.path = tmp_path / "paper-wallet.json"
        SimulatedWallet(self.path, initial={"USDC": Decimal(100), "SOL": Decimal(sol)})
        self.ledger = Ledger(tmp_path / "ledger.sqlite3")
        self.wallet = SimulatedWallet(self.path)

    def service(self) -> TradeService:
        oracle = PerpOracle("100")
        venue = SimulatedPerpsVenue(
            self.wallet, oracle, priority_fee_lamports=CAP, clock=lambda: T0
        )
        provider = paper_provider(
            self.wallet, jupiter_client=object(), priority_fee_lamports=CAP
        )
        return TradeService(
            SpotVenue(provider),
            TradeGateway(self.ledger, POLICY, False),
            mode="paper",
            clock=lambda: T0,
            prices=oracle,
            perps=venue,
        )


@pytest.fixture
def make(tmp_path):
    made: list[_Paper] = []

    def _make(sol: str) -> _Paper:
        made.append(_Paper(tmp_path, sol))
        return made[-1]

    yield _make
    for paper in made:
        paper.ledger.close()


async def _bucket(service: TradeService) -> None:
    await service.open_bucket(
        "short",
        USDC,
        SOL_MINT,
        budget_usd=Decimal(30),
        max_loss_usd=Decimal(25),
        perp=SHORT3,
    )


def _buy():
    return OrderRequest(OrderSide.BUY, Decimal("0.1"), Decimal(100), "teste")


def _sell():
    return OrderRequest(OrderSide.SELL, Decimal(1000), Decimal(100), "teste")


def test_the_room_is_about_three_hundredths_of_a_sol():
    first = open_sol_lamports(PERP_POSITION_RENT_LAMPORTS, SEND)
    assert first == 2 * PERP_REQUEST_LOCK_LAMPORTS + 1_747_520 + 3 * SEND
    # com a reserva de 0.02 SOL: ~0.0295 na primeira, ~0.0278 depois
    assert 20_000_000 + first == 29_509_800
    assert 20_000_000 + open_sol_lamports(0, SEND) == 27_762_280


async def test_a_buy_with_too_little_sol_is_refused_before_the_intent(make):
    paper = make("0.025")
    service = paper.service()
    await _bucket(service)

    reply = await service.submit_order("short", _buy())

    assert reply.status == ReplyStatus.REJECTED
    assert "0.029509800" in " ".join(reply.reasons)  # o SOL que precisa
    assert paper.ledger.list_intents() == []
    assert paper.wallet.perps() == {}


async def test_a_buy_with_three_hundredths_of_a_sol_opens(make):
    paper = make("0.03")
    service = paper.service()
    await _bucket(service)

    reply = await service.submit_order("short", _buy())

    assert reply.status == ReplyStatus.FILLED
    assert f"{SOL_MINT}:short" in paper.wallet.perps()


async def test_a_second_open_needs_no_position_rent(make):
    # depois da primeira ida e volta (o rent e três envios: abrir, stop,
    # fechar; o cancel do paper não cobra), a conta da posição existe
    paper = make("0.03")
    service = paper.service()
    await _bucket(service)
    assert (await service.submit_order("short", _buy())).status == ReplyStatus.FILLED
    assert (await service.submit_order("short", _sell())).status == ReplyStatus.FILLED
    left = paper.wallet.raw_balance(SOL_MINT)
    assert left == 30_000_000 - PERP_POSITION_RENT_LAMPORTS - 3 * SEND
    # 0.0279 SOL: abaixo da primeira abertura, acima da segunda
    assert 27_762_280 <= left < 29_509_800

    reply = await service.submit_order("short", _buy())

    assert reply.status == ReplyStatus.FILLED


async def test_the_refusal_is_a_wallet_shortfall(make):
    paper = make("0.025")
    service = paper.service()
    await _bucket(service)
    account = service._buckets["short"].account

    with pytest.raises(SolRoomError) as refused:
        await account.buy(Decimal(100), Decimal("0.1"))

    assert isinstance(refused.value, WalletShortfallError)
    assert refused.value.mint == SOL_MINT
    assert refused.value.quantity == Decimal("0.0295098")
    assert refused.value.available == Decimal("0.025")
