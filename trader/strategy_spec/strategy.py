"""`SpecStrategy`: executa uma `StrategySpec` como `TradingStrategy`.

Camada strategy: só recebe preço, saldo e posição, e devolve sinais. Não
conhece modo, chave, ledger nem política (quem limita é o gateway/bucket).

A cada tick:
1. atualiza as barras/indicadores (`IndicatorBank`) uma única vez;
2. acompanha a posição (horário de entrada, pico, horário da última saída);
3. com posição: stop (sempre) OU condições de saída; sem posição: entrada, se
   aquecido, antes da expiração e fora do cooldown.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.indicators import to_utc
from trader.models import OrderSide, OrderSignal, Position, TickerData
from trader.models.public_data import Interval
from trader.strategy_spec.conditions import IndicatorBank, TickContext, fired, holds
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.validate import parse_spec
from trader.trading_strategy import TradingStrategy

logger = logging.getLogger(__name__)

# barras guardadas no mínimo (o aquecimento é `spec.history()`)
MIN_HISTORY = 50


class SpecStrategy(TradingStrategy):
    def __init__(self, spec: StrategySpec):
        super().__init__()
        self.logger = logger
        self.spec = spec
        self.spec_id = spec.spec_id()
        # a CLI confere que o par do `run` é o da spec
        self.symbol = spec.symbol
        # aquecida só com histórico suficiente para EMA/RSI convergirem
        self._history = spec.history()
        self.bank = IndicatorBank(spec.timeframe, max(self._history, MIN_HISTORY))
        self._warm = False
        self._entry_id: str | None = None
        self._entry_px: Decimal | None = None
        self._entry_time: datetime | None = None
        self._peak: Decimal | None = None
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
        self.bank.seed(ticker_history)

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        now = to_utc(self.clock())
        self.bank.update(now, price)
        self._track(current_position, now, price)
        ctx = self._context(price, now)
        if current_position is not None:
            # saídas nunca esperam o aquecimento: o stop protege desde o
            # primeiro tick (inclusive posição restaurada após reinício);
            # condições de mercado sem dados só ficam falsas
            return self._exit(ctx, current_position)
        return self._entry(ctx, balance)

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
            self._entry_px = entry.price
            self._entry_time = to_utc(entry.timestamp)
            self._peak = entry.price
        self._peak = max(self._peak or price, price)

    def _forget_position(self) -> None:
        self._sell_px = None
        self._entry_id = None
        self._entry_px = None
        self._entry_time = None
        self._peak = None

    def _is_warm(self) -> bool:
        if not self._warm and len(self.bank) >= self._history:
            self._warm = True
            logger.debug("%s aquecida com %d barras", self.spec_id, self._history)
        return self._warm

    def _context(self, price: Decimal, now: datetime) -> TickContext:
        return TickContext(
            price=price,
            now=now,
            bank=self.bank,
            rng=self.rng,
            entry_price=self._entry_px,
            entry_time=self._entry_time,
            peak=self._peak,
            last_exit_at=self._last_exit,
            last_exit_price=self._last_exit_px,
        )

    # --- sinais ----------------------------------------------------------------

    def _exit(self, ctx: TickContext, position: Position) -> OrderSignal | None:
        stop = self.spec.exit.stop
        labels = [stop.label()] if holds(stop, ctx) else None
        labels = labels or fired(self.spec.exit.mode, self.spec.exit.conditions, ctx)
        if not labels:
            return None
        self._sell_px = ctx.price
        return self._signal(OrderSide.SELL, position.entry_order.quantity, labels)

    def _entry(self, ctx: TickContext, balance: Decimal) -> OrderSignal | None:
        if not self._may_enter(ctx.now):
            return None
        labels = fired(self.spec.entry.mode, self.spec.entry.conditions, ctx)
        if not self._armed:
            self._armed = not labels  # rearma quando a entrada deixa de valer
            return None
        usd = min(self.spec.sizing.usd, balance)
        if not labels or usd <= 0 or ctx.price <= 0:
            return None
        return self._signal(OrderSide.BUY, usd / ctx.price, labels)

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
