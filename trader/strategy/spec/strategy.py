"""`SpecStrategy`: executa uma `StrategySpec` (a única estratégia do bot).

Camada strategy: só recebe preço, saldo e posição, e devolve sinais. Não
conhece modo, chave, ledger nem política (quem limita é o gateway/bucket).

A cada tick:
1. atualiza as barras/indicadores (`IndicatorBank`) uma única vez;
2. acompanha a posição (lado, horário de entrada, pico: o melhor preço desde
   a entrada, horário da última saída);
3. com posição: stop (sempre) OU condições de saída; sem posição: entrada, se
   aquecido, antes da expiração e fora do cooldown.
"""

import logging
import random
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.shared.indicators import to_utc
from trader.shared.models import OrderSide, OrderSignal, Position, TickerData
from trader.shared.models.direction import Direction
from trader.shared.models.public_data import Interval
from trader.strategy.spec.conditions import IndicatorBank, TickContext, fired, holds
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.parse import parse_spec

logger = logging.getLogger(__name__)

# barras guardadas no mínimo (o aquecimento é `spec.history()`)
MIN_HISTORY = 50
ONE = Decimal(1)


class SpecStrategy:
    def __init__(self, spec: StrategySpec):
        # relógio e sorteios injetáveis: no backtest, o tempo do tick e uma
        # semente fixa (o resultado é determinístico)
        self.clock: Callable[[], datetime] = datetime.now
        self.rng = random.Random()
        self.spec = spec
        self.spec_id = spec.spec_id()
        self.symbol = spec.symbol
        # aquecida só com histórico suficiente para EMA/RSI convergirem
        self._history = spec.history()
        self.bank = IndicatorBank(spec.timeframe, max(self._history, MIN_HISTORY))
        self._warm = False
        self._entry_id: str | None = None
        self._entry_px: Decimal | None = None
        self._entry_time: datetime | None = None
        self._peak: Decimal | None = None
        self._direction = Direction.LONG  # o da posição aberta (D3)
        self._last_exit: datetime | None = None
        self._last_exit_px: Decimal | None = None
        # preço do último sinal de venda: vira o da saída quando a posição fecha
        self._sell_px: Decimal | None = None
        # depois de uma saída, a entrada precisa ficar falsa uma vez antes de
        # disparar de novo (senão recompra logo após o take_profit)
        self._armed = True
        # com `ttl_days`, conta do primeiro tick (no backtest, o do replay)
        self._expires_at: datetime | None = spec.expires_at

    @classmethod
    def from_file(cls, file: str) -> SpecStrategy:
        return cls(parse_spec(Path(file).read_text(encoding="utf-8")))

    def __repr__(self) -> str:
        return f"SpecStrategy({self.spec.name} {self.spec_id} {self.symbol})"

    def set_clock(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    def seed(self, seed: int | str | None) -> None:
        self.rng = random.Random(seed)

    def resume(
        self,
        last_exit_at: datetime | None,
        opened_at: datetime | None,
        last_exit_price: Decimal | None = None,
    ) -> None:
        """Retoma cooldown, rearme, validade e preço de saída do ledger."""
        self._last_exit_px = last_exit_price
        if last_exit_at is not None:
            self._last_exit = to_utc(last_exit_at)
            self._armed = False
        if opened_at is not None and self.spec.expires_at is None:
            self._expires_at = self.spec.expiry(opened_at)

    def warmup(self) -> tuple[Interval, int]:
        """Timeframe e quantidade de candles para aquecer os indicadores."""
        return self.spec.timeframe, self._history

    def setup(self, ticker_history: Sequence[TickerData]):
        # os candles acabam no último negócio, não agora (A4)
        self.bank.seed(ticker_history, until=self.clock())

    def on_market_refresh(
        self,
        price: Decimal,
        balance: Decimal,
        current_position: Position | None,
        quote_usd: Decimal | None,
    ) -> OrderSignal | None:
        """Preço e saldo no token de cotação; `quote_usd` converte o `fixed_usd`."""
        now = to_utc(self.clock())
        self.bank.update(now, price)
        self._track(current_position, now, price)
        ctx = self._context(price, now)
        if current_position is not None:
            # saídas nunca esperam o aquecimento: o stop protege desde o
            # primeiro tick (inclusive posição restaurada após reinício);
            # condições de mercado sem dados só ficam falsas
            return self._exit(ctx, current_position)
        return self._entry(ctx, balance, quote_usd)

    # --- estado da posição ---------------------------------------------------

    def _track(self, position: Position | None, now: datetime, price: Decimal):
        if position is None:
            if self._entry_id is not None:
                self._last_exit = now  # posição fechou: começa o cooldown
                self._last_exit_px = self._sell_px or price
                self._armed = False
                logger.debug("%s saiu a %s", self.spec_id, self._last_exit_px)
            self._forget_position()
            return
        entry = position.entry_order
        if entry.order_id != self._entry_id:
            # posição nova (ou restaurada após reinício): o horário vem da
            # ordem, que usa o relógio da conta (tick no backtest); o pico
            # começa na entrada, para o trailing stop respeitar a perda máxima
            self._entry_id = entry.order_id
            self._entry_px = entry.quote_price
            self._entry_time = to_utc(entry.timestamp)
            self._direction = position.direction
            self._peak = self._entry_px
        # o melhor preço desde a entrada: o maior comprado, o menor vendido
        self._peak = self._direction.better(self._peak or price, price)

    def _forget_position(self) -> None:
        self._sell_px = None
        self._entry_id = None
        self._entry_px = None
        self._entry_time = None
        self._peak = None
        self._direction = Direction.LONG

    def _is_warm(self) -> bool:
        warm = len(self.bank) >= self._history
        if warm != self._warm:
            # esfria depois de um buraco no feed (a série recomeçou)
            logger.debug("%s %s", self.spec_id, "aquecida" if warm else "esfriou")
            self._warm = warm
        return warm

    def _context(self, price: Decimal, now: datetime) -> TickContext:
        return TickContext(
            price=price,
            now=now,
            bank=self.bank,
            rng=self.rng,
            entry_price=self._entry_px,
            entry_time=self._entry_time,
            peak=self._peak,
            direction=self._direction,
            last_exit_at=self._last_exit,
            last_exit_price=self._last_exit_px,
        )

    # --- sinais ----------------------------------------------------------------

    def _exit(self, ctx: TickContext, position: Position) -> OrderSignal | None:
        stop = self.spec.exit.stop
        labels = [stop.label()] if holds(stop, ctx) else None
        labels = labels or fired(self.spec.exit.mode, self.spec.exit.conditions, ctx)
        if not labels:
            return self._partial(ctx, position)
        self._sell_px = ctx.price
        return self._signal(OrderSide.SELL, position.entry_order.quantity, labels)

    def _partial(self, ctx: TickContext, position: Position) -> OrderSignal | None:
        """A saída parcial da spec (A12): `pct`% da entrada, uma vez por posição
        (uma que já teve uma venda parcial não reduz de novo, A20)."""
        partial = self.spec.exit.partial
        entry = position.entry_order
        if partial is None or position.partial_sells:
            return None
        labels = fired(partial.mode, partial.conditions, ctx)
        if not labels:
            return None
        quantity = entry.quantity * partial.pct / 100
        return self._signal(
            OrderSide.SELL, quantity, [f"partial{partial.pct}%", *labels]
        )

    def _entry(
        self, ctx: TickContext, balance: Decimal, quote_usd: Decimal | None
    ) -> OrderSignal | None:
        if not self._may_enter(ctx.now):
            return None
        labels = fired(self.spec.entry.mode, self.spec.entry.conditions, ctx)
        if not self._armed:
            self._armed = not labels  # rearma quando a entrada deixa de valer
            return None
        spend = self.spec.sizing.spend(balance, quote_usd)  # em cotação
        if not labels or spend <= 0 or ctx.price <= 0:
            return None
        return self._signal(OrderSide.BUY, spend / ctx.price, labels)

    def _may_enter(self, now: datetime) -> bool:
        if self._expires_at is None:
            self._expires_at = self.spec.expiry(now)
        if not self._is_warm() or now >= self._expires_at:
            return False
        cooldown = timedelta(minutes=self.spec.cooldown_minutes)
        return self._last_exit is None or now >= self._last_exit + cooldown

    def _signal(
        self, side: OrderSide, quantity: Decimal, labels: list[str]
    ) -> OrderSignal:
        rationale = f"spec {self.spec_id} {side}: {', '.join(labels)}"
        logger.debug("%s", rationale)
        return OrderSignal(side, quantity, rationale=rationale)
