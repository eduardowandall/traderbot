"""Backtest determinístico: reproduz ticks gravados por uma estratégia.

Usa o mesmo caminho de execução do paper trading (`TradeService` ->
`TradeGateway` -> `AsyncJupiterProvider` + `SimulatedExecutor` ->
`SimulatedWallet`), com um gateway em memória sem limites de política; só a
quote é sintética, calculada a partir do preço do tick com uma taxa
(`fee_bps`). A taxa de rede fica fora do fill, como ao vivo (A14): o
`ReplayExecutor` a informa como custo da perna. A estratégia recebe o
relógio do replay e uma semente fixa, então a mesma entrada gera sempre o
mesmo resultado.

O preço do tick é o do token no token de cotação (USD num par USDC/USDT).
Num par sem stablecoin (ex: JUP-SOL) cada tick traz também o preço USD do token
de cotação (`Tick.quote_usd`): a carteira começa com `budget_usd` convertido,
o serviço vê os dois preços em USD por um oráculo do replay (orçamento e PnL
em USD, como ao vivo), e o patrimônio é medido em USD.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from trader.backtest.ticks import Tick
from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.jupiter.async_jupiter_svc import (
    AsyncJupiterProvider,
)
from trader.execution.trade.venues.paper.executor import SimulatedExecutor
from trader.execution.trade.venues.paper.wallet import SimulatedWallet
from trader.shared.indicators import to_utc
from trader.shared.models import SOLANA_MINTS, Mint, OrderSide, SwapResult, TickerData
from trader.shared.models.costs import (
    BPS,
    LAMPORTS_PER_SOL,
    REPLAY,
    RoundTripCosts,
)
from trader.shared.models.mints import SOL_MINT
from trader.strategy.bot.config import Strategy
from trader.strategy.bot.decision import bucket_done, order_for

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
ONE = Decimal("1")
# o SOL do replay num par sem SOL: só carrega a taxa de rede (um valor em USD)
REPLAY_SOL_USD = Decimal(150)

# loggers das estratégias: silenciados durante o replay (milhares de ticks
# viram milhares de linhas; o resultado já resume o que aconteceu)
STRATEGY_LOGGERS = ("trader.strategy.spec",)


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
    """Substitui o `AsyncJupiterClient` do provider: quotes do tick atual.

    A saída é o tick menos `fee_bps` (taxa do pool + slippage); a taxa de rede
    não sai daqui, então o fill é o que seria ao vivo (A14).
    """

    def __init__(self, quote_mint: Mint, fee_bps: Decimal):
        self.quote_mint = quote_mint  # stablecoin (input do par)
        self.fee_bps = fee_bps
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
        if input_mint == self.quote_mint.mint:
            out_ui = in_ui / price  # compra do token
        else:
            out_ui = in_ui * price  # venda do token
        out_ui = out_ui * (BPS - self.fee_bps) / BPS
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
    """O oráculo de preços USD do replay: o token, a cotação e o SOL.

    O SOL é o do tick quando um lado do par é SOL; senão `REPLAY_SOL_USD`. A
    taxa de rede do replay é um valor em USD e os lamports só a carregam, então
    a referência muda o número em SOL mostrado, nunca um valor em USD.
    """

    def __init__(self, client: ReplayQuoteClient, token: Mint, quote: Mint):
        self.client = client
        self.token = token
        self.quote = quote

    def _known(self) -> dict[str, Decimal]:
        assert self.client.tick is not None
        quote_usd = self.client.quote_usd
        # o par sobrescreve o SOL de referência quando um lado dele é SOL
        return {
            SOL_MINT: REPLAY_SOL_USD,
            self.quote.mint: quote_usd,
            self.token.mint: self.client.tick.price * quote_usd,
        }

    def sol_usd(self) -> Decimal:
        return self._known()[SOL_MINT]

    async def usd_prices(self, mints) -> dict[str, Decimal]:
        known = self._known()
        return {mint: known[mint] for mint in mints if mint in known}


class ReplayExecutor(SimulatedExecutor):
    """O executor do replay: o fill da quote e a taxa de rede como custo.

    Ao vivo a taxa é paga em SOL, fora do fill; aqui ela vira `fee_lamports`
    da perna (`network_fee_usd` ao SOL do replay), e o ledger a desconta do
    PnL como ao vivo. A carteira do replay não tem SOL: o executor soma as
    taxas em `fees_usd`, que o patrimônio desconta.
    """

    def __init__(
        self,
        wallet: SimulatedWallet,
        network_fee_usd: Decimal,
        sol_usd: Callable[[], Decimal],
    ):
        super().__init__(
            wallet,
            fee_lamports=0,
            account_rent_lamports=0,
            cost_source=REPLAY,
            slippage_bps=0,  # já na quote (`slippage_bps` do backtest)
        )
        self.network_fee_usd = network_fee_usd
        self.sol_usd = sol_usd
        self.fees_usd = ZERO

    async def execute(
        self, input_mint: str, output_mint: str, quote: JupiterQuoteResponse
    ) -> SwapResult:
        result = await super().execute(input_mint, output_mint, quote)
        assert result.costs is not None  # o `SimulatedExecutor` sempre preenche
        sol_usd = self.sol_usd()
        lamports = int(self.network_fee_usd / sol_usd * LAMPORTS_PER_SOL)
        costs = replace(result.costs, fee_lamports=lamports)
        # o que o ledger desconta (lamports truncados), não `network_fee_usd`
        self.fees_usd += costs.native_cost_sol * sol_usd
        return replace(result, costs=costs)


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
        # taxa de rede por perna, em USD: custo da perna, fora do fill (A14)
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
        client, executor, service = self._venue(gateway)
        client.tick = self.ticks[0]  # abrir o bucket já lê saldo no relógio do replay
        await service.open_bucket(
            self.symbol,
            self.quote.mint,
            self.token.mint,
            budget_usd=self.budget_usd,
            source="backtest",
            max_loss_usd=self.max_loss_usd,
        )
        self.strategy.set_clock(lambda: client.now)
        self.strategy.seed(self.seed)
        if self.warmup:
            self.strategy.setup(self.warmup)

        run = _Run()
        for tick in self.ticks:
            client.tick = tick
            if not await self._step(service, tick, run):
                break  # bucket encerrado e sem posição: ao vivo, o bot pararia
            run.track(self._equity(executor, tick))

        final = await service.get_bucket(self.symbol)
        return BacktestResult(
            symbol=self.symbol,
            # em UTC, como o ledger: ticks de candles vêm em hora local (F13)
            start=to_utc(self.ticks[0].timestamp),
            end=to_utc(self.ticks[-1].timestamp),
            ticks=len(self.ticks),
            initial_equity=self.initial_balance,
            final_equity=self._equity(executor, self.ticks[-1]),
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
    ) -> tuple[ReplayQuoteClient, ReplayExecutor, TradeService]:
        """Mesmo caminho do paper trading: TradeService -> gateway -> carteira."""
        client = ReplayQuoteClient(self.quote, self.fee_bps + self.slippage_bps)
        prices = ReplayPrices(client, self.token, self.quote)
        wallet = SimulatedWallet(initial={self.quote.symbol: self._funding()})
        executor = ReplayExecutor(wallet, self.network_fee_usd, prices.sol_usd)
        # a quote sintética não tem impacto de preço
        provider = AsyncJupiterProvider(executor, client, max_price_impact_pct=None)
        # ledger em memória e política sem limites; ordens no horário do tick;
        # os preços USD vêm do tick (orçamento e PnL)
        service = TradeService(
            provider, gateway, clock=lambda: client.now, prices=prices
        )
        return client, executor, service

    async def _step(self, service: TradeService, tick: Tick, run: _Run) -> bool:
        """Um tick, com a mesma decisão do bot ao vivo; False: o replay acabou."""
        snapshot = await service.get_bucket(self.symbol)
        if bucket_done(snapshot):
            return False
        request = order_for(self.strategy, tick.price, snapshot)
        if request is None:
            return True
        reply = await service.submit_order(self.symbol, request)
        order = reply.order
        if order is None:
            run.rejected += 1
            logger.debug(f"sinal recusado em {tick.timestamp}: {reply}")
            return True
        realized = None
        if order.side == OrderSide.SELL:
            after = await service.get_bucket(self.symbol)
            realized = after.realized_usd - snapshot.realized_usd
        run.trades.append(
            BacktestTrade(
                to_utc(tick.timestamp),
                order.side,
                order.quantity,
                order.price,
                realized,
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

    def _equity(self, executor: ReplayExecutor, tick: Tick) -> Decimal:
        """Patrimônio em USD: cotação + token ao preço do tick, a `quote_usd`,
        menos as taxas de rede pagas (a carteira do replay não tem SOL)."""
        wallet = executor.wallet
        in_quote = (
            wallet.balance(self.quote.mint)
            + wallet.balance(self.token.mint) * tick.price
        )
        return in_quote * (tick.quote_usd or ONE) - executor.fees_usd
