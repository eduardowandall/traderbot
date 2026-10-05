"""Backtest determinístico: reproduz ticks gravados por uma estratégia.

Usa o mesmo caminho de execução do paper trading (`TradeService` ->
`TradeGateway` -> `AsyncJupiterProvider` + `SimulatedExecutor` ->
`SimulatedWallet`), com um gateway em memória sem limites de política; só a
quote é sintética, calculada a partir do preço do tick com uma taxa
(`fee_bps`). A estratégia recebe o
relógio do replay e uma semente fixa, então a mesma entrada gera sempre o
mesmo resultado.

O preço do tick é o do token no token de cotação (USD num par USDC/USDT).
Num par sem stablecoin (ex: JUP-SOL) cada tick traz também o preço USD do token
de cotação (`Tick.quote_usd`): a carteira começa com `budget_usd` convertido,
o serviço vê os dois preços em USD por um oráculo do replay (orçamento e PnL
em USD, como ao vivo), e o patrimônio é medido em USD.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from trader.backtest.ticks import Tick
from trader.bot.config import Strategy
from trader.bot.decision import bucket_done, order_for
from trader.execution import TradeGateway
from trader.models import SOLANA_MINTS, Mint, OrderSide, TickerData
from trader.models.costs import BPS, REPLAY, RoundTripCosts
from trader.paper.provider import paper_provider
from trader.paper.wallet import SimulatedWallet
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.trading_service.local import LocalTradeClient
from trader.trading_service.service import TradeService

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
ONE = Decimal("1")

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

    def __init__(
        self, quote_mint: Mint, fee_bps: Decimal, network_fee_usd: Decimal = ZERO
    ):
        self.quote_mint = quote_mint  # stablecoin (input do par)
        self.fee_bps = fee_bps
        # taxa de rede por perna, em USD, tirada da saída (como custo do swap)
        self.network_fee_usd = network_fee_usd
        self.tick: Tick | None = None

    @property
    def now(self) -> datetime:
        assert self.tick is not None, "replay ainda não começou"
        return self.tick.timestamp

    @property
    def quote_usd(self) -> Decimal:
        assert self.tick is not None, "replay ainda não começou"
        return ONE if self.tick.quote_usd is None else self.tick.quote_usd

    async def get_quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50
    ) -> JupiterQuoteResponse:
        assert self.tick is not None
        price = self.tick.price
        in_ui = SOLANA_MINTS.raw_to_ui(input_mint, amount)
        # a taxa de rede em unidades do token de cotação
        fee_in_quote = self.network_fee_usd / self.quote_usd
        if input_mint == self.quote_mint.mint:
            out_ui = in_ui / price  # compra do token
            network_fee = fee_in_quote / price
        else:
            out_ui = in_ui * price  # venda do token
            network_fee = fee_in_quote
        out_ui = max(ZERO, out_ui * (BPS - self.fee_bps) / BPS - network_fee)
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


class ReplayPrices:
    """O oráculo de preços USD do replay: o token e a cotação, do tick atual."""

    def __init__(self, client: ReplayQuoteClient, token: Mint, quote: Mint):
        self.client = client
        self.token = token
        self.quote = quote

    async def usd_prices(self, mints) -> dict[str, Decimal]:
        assert self.client.tick is not None
        quote_usd = self.client.quote_usd
        known = {
            self.quote.mint: quote_usd,
            self.token.mint: self.client.tick.price * quote_usd,
        }
        return {mint: known[mint] for mint in mints if mint in known}


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
    # do ledger do replay, como nos relatórios ao vivo
    round_trip_costs: RoundTripCosts = field(default_factory=RoundTripCosts)

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
        strategy: Strategy,
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
        # taxa de rede por perna, em USD (o executor do replay não tem SOL)
        network_fee_usd: Decimal = ZERO,
        # candles de aquecimento antes do primeiro tick, como o bot faz ao
        # iniciar (`strategy.setup`); vazio: aquece nos próprios ticks
        warmup: Sequence[TickerData] = (),
    ):
        self.token, self.quote = SOLANA_MINTS.get_pair(symbol)
        if not ticks:
            raise ValueError("Nenhum tick para reproduzir")
        if not self.quote.is_usd_stable and ticks[0].quote_usd is None:
            raise ValueError(
                f"{symbol}: os ticks precisam do preço USD de {self.quote.symbol} "
                "(a terceira coluna do CSV, ou candles das duas séries)"
            )
        if min(fee_bps, slippage_bps, network_fee_usd) < 0:
            raise ValueError("custos do backtest não podem ser negativos")
        self.symbol = symbol
        self.strategy = strategy
        self.ticks = ticks
        self.initial_balance = initial_balance
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.network_fee_usd = network_fee_usd
        self.warmup = warmup
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
        client.tick = self.ticks[0]  # abrir o bucket já lê saldo no relógio do replay
        await trader.open()
        self.strategy.set_clock(lambda: client.now)
        self.strategy.seed(self.seed)
        if self.warmup:
            self.strategy.setup(self.warmup)

        run = _Run()
        for tick in self.ticks:
            client.tick = tick
            if not await self._step(trader, tick, run):
                break  # bucket encerrado e sem posição: ao vivo, o bot pararia
            run.track(self._equity(wallet, tick))

        final = await trader.bucket()
        return BacktestResult(
            symbol=self.symbol,
            start=self.ticks[0].timestamp,
            end=self.ticks[-1].timestamp,
            ticks=len(self.ticks),
            initial_equity=self.initial_balance,
            final_equity=self._equity(wallet, self.ticks[-1]),
            max_drawdown_pct=run.max_drawdown,
            realized_pnl=final.realized_usd,
            rejected_signals=run.rejected,
            open_position=final.position is not None,
            trades=run.trades,
            # o bucket do replay é o par (serviço sem modo)
            round_trip_costs=gateway.ledger.round_trip_costs(self.symbol),
        )

    def _venue(
        self, gateway: TradeGateway
    ) -> tuple[ReplayQuoteClient, SimulatedWallet, LocalTradeClient]:
        """Mesmo caminho do paper trading: TradeService -> gateway -> carteira."""
        client = ReplayQuoteClient(
            self.quote, self.fee_bps + self.slippage_bps, self.network_fee_usd
        )
        wallet = SimulatedWallet(initial={self.quote.symbol: self._funding()})
        provider = paper_provider(
            wallet,
            jupiter_client=client,
            # a quote sintética não tem impacto de preço; a taxa de rede sai
            # da quote em USD (a carteira do backtest só tem a stablecoin), e
            # sem taxas em SOL o executor não reserva SOL
            max_price_impact_pct=None,
            fee_lamports=0,
            account_rent_lamports=0,
            cost_source=REPLAY,  # custos modelados na quote do replay
            slippage_bps=0,  # já na quote (`slippage_bps` do backtest)
        )
        # ledger em memória e política sem limites; ordens no horário do tick;
        # os preços USD vêm do tick (orçamento e PnL)
        prices = ReplayPrices(client, self.token, self.quote)
        service = TradeService(
            provider, gateway, clock=lambda: client.now, prices=prices
        )
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

    async def _step(self, trader: LocalTradeClient, tick: Tick, run: _Run) -> bool:
        """Um tick, com a mesma decisão do bot ao vivo; False: o replay acabou."""
        snapshot = await trader.bucket()
        if bucket_done(snapshot):
            return False
        request = order_for(self.strategy, tick.price, snapshot)
        if request is None:
            return True
        reply = await trader.submit(request)
        order = reply.order
        if order is None:
            run.rejected += 1
            logger.debug(f"sinal recusado em {tick.timestamp}: {reply}")
            return True
        realized = None
        if order.side == OrderSide.SELL:
            realized = (await trader.bucket()).realized_usd - snapshot.realized_usd
        run.trades.append(
            BacktestTrade(
                tick.timestamp, order.side, order.quantity, order.price, realized
            )
        )
        return True

    def _funding(self) -> Decimal:
        """O saldo inicial no token de cotação: o orçamento em USD convertido.

        Arredondado para cima na menor unidade do token: truncado, o saldo
        ficaria um tico abaixo do orçamento e o bucket não abriria.
        """
        start_usd = self.ticks[0].quote_usd
        if start_usd is None:
            return self.initial_balance
        unit = Decimal(1).scaleb(-self.quote.decimals)
        return (self.initial_balance / start_usd).quantize(unit, ROUND_CEILING)

    def _equity(self, wallet: SimulatedWallet, tick: Tick) -> Decimal:
        """Patrimônio em USD: cotação + token ao preço do tick, a `quote_usd`."""
        in_quote = (
            wallet.balance(self.quote.mint)
            + wallet.balance(self.token.mint) * tick.price
        )
        return in_quote * (tick.quote_usd or ONE)
