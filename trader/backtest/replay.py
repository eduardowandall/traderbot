"""Backtest determinístico: reproduz ticks gravados por uma estratégia.

Usa o mesmo caminho de execução do paper trading (`TradeService` ->
`TradeGateway` -> `AsyncJupiterProvider` + `SimulatedExecutor` ->
`SimulatedWallet`), com um gateway em memória sem limites de política; só a
quote é sintética, calculada a partir do preço do tick com uma taxa
(`fee_bps`). A estratégia recebe o
relógio do replay e uma semente fixa, então a mesma entrada gera sempre o
mesmo resultado.

Limitação: o preço do feed é em USD, então o token de entrada do par precisa
ser uma stablecoin de dólar (ex: SOL-USDC).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trader.backtest.ticks import Tick
from trader.execution import TradeGateway
from trader.models import SOLANA_MINTS, Mint, OrderSide
from trader.models.costs import REPLAY
from trader.paper.provider import paper_provider
from trader.paper.wallet import SimulatedWallet
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.trading_service.local import LocalTradeClient
from trader.trading_service.protocol import BucketStatus, OrderRequest
from trader.trading_service.service import TradeService
from trader.trading_strategy import TradingStrategy

logger = logging.getLogger(__name__)

BPS = Decimal("10000")

# loggers das estratégias: silenciados durante o replay (milhares de ticks
# viram milhares de linhas; o resultado já resume o que aconteceu)
STRATEGY_LOGGERS = ("trader.strategy_spec",)


@contextmanager
def quiet_strategy_logs() -> Iterator[None]:
    loggers = [logging.getLogger(name) for name in STRATEGY_LOGGERS]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous, strict=True):
            lg.setLevel(level)


class ReplayQuoteClient:
    """Substitui o `AsyncJupiterClient` do provider: quotes do tick atual."""

    def __init__(self, quote_mint: Mint, fee_bps: Decimal):
        self.quote_mint = quote_mint  # stablecoin (input do par)
        self.fee_bps = fee_bps
        self.tick: Tick | None = None

    @property
    def now(self) -> datetime:
        assert self.tick is not None, "replay ainda não começou"
        return self.tick.timestamp

    async def get_quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50
    ) -> JupiterQuoteResponse:
        assert self.tick is not None
        price = self.tick.price
        in_ui = SOLANA_MINTS.raw_to_ui(input_mint, amount)
        if input_mint == self.quote_mint.mint:
            out_ui = in_ui / price  # compra do token
        else:
            out_ui = in_ui * price  # venda do token
        out_ui *= (BPS - self.fee_bps) / BPS
        return JupiterQuoteResponse.single_route(
            input_mint,
            amount,
            output_mint,
            SOLANA_MINTS.ui_to_raw(output_mint, out_ui),
            slippage_bps=slippage_bps,
            label="replay",
        )

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class BacktestTrade:
    timestamp: datetime
    side: OrderSide
    quantity: Decimal  # do token
    price: Decimal  # USD por token (fill)
    realized_pnl: Decimal | None = None


@dataclass
class BacktestResult:
    symbol: str
    start: datetime
    end: datetime
    ticks: int
    initial_equity: Decimal
    final_equity: Decimal
    max_drawdown_pct: Decimal
    realized_pnl: Decimal
    rejected_signals: int
    open_position: bool
    trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def return_pct(self) -> Decimal:
        if not self.initial_equity:
            return Decimal("0")
        return (self.final_equity / self.initial_equity - 1) * 100

    @property
    def closed_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.realized_pnl is not None]

    @property
    def win_rate_pct(self) -> Decimal | None:
        closed = self.closed_trades
        if not closed:
            return None
        wins = sum(1 for t in closed if t.realized_pnl and t.realized_pnl > 0)
        return Decimal(wins) / Decimal(len(closed)) * 100

    def summary(self) -> str:
        win_rate = self.win_rate_pct
        lines = [
            f"Backtest {self.symbol}: {self.ticks} ticks ({self.start} -> {self.end})",
            f"  patrimônio: {self.initial_equity:.4f} -> {self.final_equity:.4f} USD "
            f"({self.return_pct:+.2f}%)",
            f"  PnL realizado: {self.realized_pnl:+.4f} USD",
            f"  drawdown máximo: {self.max_drawdown_pct:.2f}%",
            f"  trades: {len(self.trades)} ({len(self.closed_trades)} fechados, "
            f"win rate {'-' if win_rate is None else f'{win_rate:.1f}%'})",
            f"  sinais recusados: {self.rejected_signals}",
            f"  posição aberta no fim: {'sim' if self.open_position else 'não'}",
        ]
        return "\n".join(lines)


@dataclass
class _Run:
    """Acumuladores de um replay."""

    trades: list[BacktestTrade] = field(default_factory=list)
    rejected: int = 0
    peak: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")

    def track(self, equity: Decimal) -> None:
        self.peak = max(self.peak, equity)
        if self.peak > 0:
            drawdown = (self.peak - equity) / self.peak * 100
            self.max_drawdown = max(self.max_drawdown, drawdown)


class Backtester:
    def __init__(
        self,
        strategy: TradingStrategy,
        symbol: str,
        ticks: list[Tick],
        initial_balance: Decimal,
        fee_bps: Decimal = Decimal("30"),
        # custo de execução além da taxa (desvio do preço do tick), por perna
        slippage_bps: Decimal = Decimal("10"),
        seed: int | str = 0,
        # teto do bucket, como ao vivo (prejuízo realizado reduz o disponível)
        budget_usd: Decimal | None = None,
        # prejuízo que encerra o bucket, como ao vivo
        max_loss_usd: Decimal | None = None,
    ):
        self.token, self.quote = SOLANA_MINTS.get_pair(symbol)
        if not self.quote.is_usd_stable:
            raise ValueError(
                f"Backtest precisa de uma stablecoin de dólar como entrada "
                f"(ex: {self.token.symbol}-USDC); {self.quote.symbol} não é"
            )
        if not ticks:
            raise ValueError("Nenhum tick para reproduzir")
        if fee_bps < 0 or slippage_bps < 0:
            raise ValueError("fee_bps e slippage_bps não podem ser negativos")
        self.symbol = symbol
        self.strategy = strategy
        self.ticks = ticks
        self.initial_balance = initial_balance
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.seed = seed
        self.budget_usd = budget_usd
        self.max_loss_usd = max_loss_usd

    async def run(self) -> BacktestResult:
        with quiet_strategy_logs():
            return await self._replay()

    async def _replay(self) -> BacktestResult:
        with TradeGateway.in_memory() as gateway:
            return await self._replay_on(gateway)

    async def _replay_on(self, gateway: TradeGateway) -> BacktestResult:
        client, wallet, trader = self._venue(gateway)
        await trader.open()
        self.strategy.set_clock(lambda: client.now)
        self.strategy.seed(self.seed)

        run = _Run()
        for tick in self.ticks:
            client.tick = tick
            await self._step(trader, tick, run)
            run.track(self._equity(wallet, tick.price))

        final = await trader.bucket()
        return BacktestResult(
            symbol=self.symbol,
            start=self.ticks[0].timestamp,
            end=self.ticks[-1].timestamp,
            ticks=len(self.ticks),
            initial_equity=self.initial_balance,
            final_equity=self._equity(wallet, self.ticks[-1].price),
            max_drawdown_pct=run.max_drawdown,
            realized_pnl=final.realized_usd,
            rejected_signals=run.rejected,
            open_position=final.position is not None,
            trades=run.trades,
        )

    def _venue(
        self, gateway: TradeGateway
    ) -> tuple[ReplayQuoteClient, SimulatedWallet, LocalTradeClient]:
        """Mesmo caminho do paper trading: TradeService -> gateway -> carteira."""
        client = ReplayQuoteClient(self.quote, self.fee_bps + self.slippage_bps)
        wallet = SimulatedWallet(initial={self.quote.symbol: self.initial_balance})
        provider = paper_provider(
            wallet,
            jupiter_client=client,
            # a quote sintética não tem impacto de preço; taxas de rede em SOL
            # não se aplicam (a carteira do backtest só tem a stablecoin), e
            # sem taxas o executor não reserva SOL
            max_price_impact_pct=None,
            fee_lamports=0,
            account_rent_lamports=0,
            cost_source=REPLAY,  # custos modelados em fee_bps
        )
        # ledger em memória e política sem limites; ordens no horário do tick
        service = TradeService(provider, gateway, clock=lambda: client.now)
        trader = LocalTradeClient(
            service,
            self.symbol,
            self.quote.mint,
            self.token.mint,
            budget_usd=self.budget_usd,
            max_loss_usd=self.max_loss_usd,
            source="backtest",
        )
        return client, wallet, trader

    async def _step(self, trader: LocalTradeClient, tick: Tick, run: _Run) -> None:
        snapshot = await trader.bucket()
        if snapshot.status != BucketStatus.ACTIVE:
            return  # bucket encerrado (perda máxima): ao vivo, o bot pararia
        signal = self.strategy.on_market_refresh(
            tick.price, None, snapshot.available_usd, snapshot.position
        )
        if signal is None:
            return
        reply = await trader.submit(
            OrderRequest(signal.side, signal.quantity, tick.price, signal.rationale)
        )
        order = reply.order
        if order is None:
            run.rejected += 1
            logger.debug(f"sinal recusado em {tick.timestamp}: {reply}")
            return
        realized = None
        if order.side == OrderSide.SELL:
            realized = (await trader.bucket()).realized_usd - snapshot.realized_usd
        run.trades.append(
            BacktestTrade(
                tick.timestamp, order.side, order.quantity, order.price, realized
            )
        )

    def _equity(self, wallet: SimulatedWallet, price: Decimal) -> Decimal:
        return wallet.balance(self.quote.mint) + wallet.balance(self.token.mint) * price
