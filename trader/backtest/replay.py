"""Backtest determinístico: reproduz ticks gravados por uma estratégia.

Usa o mesmo caminho de execução do paper trading (`AsyncAccount` →
`PaperJupiterProvider` → `SimulatedWallet`); só a quote é sintética, calculada
a partir do preço do tick com uma taxa (`fee_bps`). A estratégia recebe o
relógio do replay e uma semente fixa, então a mesma entrada gera sempre o
mesmo resultado.

Limitação: o preço do feed é em USD, então o token de entrada do par precisa
ser uma stablecoin de dólar (ex: SOL-USDC).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trader.async_account import AsyncAccount
from trader.backtest.ticks import Tick
from trader.models import SOLANA_MINTS, Mint, OrderSide
from trader.models.costs import REPLAY
from trader.paper.provider import PaperJupiterProvider
from trader.paper.wallet import SimulatedWallet
from trader.providers.jupiter.async_jupiter_svc import SwapRejectedError
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.trading_strategy import TradingStrategy

logger = logging.getLogger(__name__)

BPS = Decimal("10000")


class ReplayQuoteClient:
    """Substitui o `AsyncJupiterClient`: preço e quotes vêm do tick atual."""

    def __init__(self, quote_mint: Mint, fee_bps: Decimal):
        self.quote_mint = quote_mint  # stablecoin (input do par)
        self.fee_bps = fee_bps
        self.tick: Tick | None = None

    @property
    def now(self) -> datetime:
        assert self.tick is not None, "replay ainda não começou"
        return self.tick.timestamp

    async def get_price(self, mint: str) -> Decimal:
        assert self.tick is not None
        return self.tick.price

    async def get_candles(self, mint: str, **kwargs) -> list:
        return []

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


class Backtester:
    def __init__(
        self,
        strategy: TradingStrategy,
        symbol: str,
        ticks: list[Tick],
        initial_balance: Decimal,
        fee_bps: Decimal = Decimal("30"),
        seed: int | str = 0,
    ):
        self.token, self.quote = SOLANA_MINTS.get_pair(symbol)
        if not self.quote.is_usd_stable:
            raise ValueError(
                f"Backtest precisa de uma stablecoin de dólar como entrada "
                f"(ex: {self.token.symbol}-USDC); {self.quote.symbol} não é"
            )
        if not ticks:
            raise ValueError("Nenhum tick para reproduzir")
        self.symbol = symbol
        self.strategy = strategy
        self.ticks = ticks
        self.initial_balance = initial_balance
        self.fee_bps = fee_bps
        self.seed = seed

    async def run(self) -> BacktestResult:
        client = ReplayQuoteClient(self.quote, self.fee_bps)
        wallet = SimulatedWallet(initial={self.quote.symbol: self.initial_balance})
        provider = PaperJupiterProvider(
            wallet,
            jupiter_client=client,
            # a quote sintética não tem impacto de preço; taxas de rede em SOL
            # não se aplicam (a carteira do backtest só tem a stablecoin)
            max_price_impact_pct=None,
            fee_lamports=0,
            account_rent_lamports=0,
            cost_source=REPLAY,  # custos modelados em fee_bps
        )
        account = AsyncAccount(
            provider,
            self.quote.pubkey,
            self.token.pubkey,
            # sem taxas de rede no replay: não precisa reservar SOL
            sol_fee_reserve=Decimal("0"),
            source="backtest",
        )
        self.strategy.set_clock(lambda: client.now)
        self.strategy.seed(self.seed)

        trades: list[BacktestTrade] = []
        rejected = 0
        peak = Decimal("0")
        max_drawdown = Decimal("0")

        for tick in self.ticks:
            client.tick = tick
            balance = await account.get_balance(self.quote.pubkey)
            position = account.get_position()
            signal = self.strategy.on_market_refresh(
                tick.price, None, balance, position
            )
            if signal:
                pnl_before = account.get_total_realized_pnl()
                try:
                    order = await account.place_order(
                        tick.price, signal.side, signal.quantity
                    )
                    realized = (
                        account.get_total_realized_pnl() - pnl_before
                        if order.side == OrderSide.SELL
                        else None
                    )
                    trades.append(
                        BacktestTrade(
                            tick.timestamp,
                            order.side,
                            order.quantity,
                            order.price,
                            realized,
                        )
                    )
                except (ValueError, SwapRejectedError, RuntimeError) as ex:
                    rejected += 1
                    logger.debug(f"sinal recusado em {tick.timestamp}: {ex}")

            equity = self._equity(wallet, tick.price)
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)

        return BacktestResult(
            symbol=self.symbol,
            start=self.ticks[0].timestamp,
            end=self.ticks[-1].timestamp,
            ticks=len(self.ticks),
            initial_equity=self.initial_balance,
            final_equity=self._equity(wallet, self.ticks[-1].price),
            max_drawdown_pct=max_drawdown,
            realized_pnl=account.get_total_realized_pnl(),
            rejected_signals=rejected,
            open_position=account.get_position() is not None,
            trades=trades,
        )

    def _equity(self, wallet: SimulatedWallet, price: Decimal) -> Decimal:
        return wallet.balance(self.quote.mint) + wallet.balance(self.token.mint) * price
