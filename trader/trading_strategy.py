import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from trader.models.public_data import TickerData

from .models import OrderSide, OrderSignal, Position

_UNSET = object()


class TradingStrategy(ABC):
    """Classe base para estratégias de trading"""

    # relógio e gerador aleatório injetáveis: no replay/backtest usam o tempo
    # dos ticks e uma semente fixa, para o resultado ser determinístico.
    # Definidos por instância em __init__ (subclasses devem chamá-lo).
    clock: Callable[[], datetime]
    rng: random.Random

    def __init__(self):
        self.logger = logging.getLogger(self.__module__)
        self.clock = datetime.now
        self.rng = random.Random()
        # último estado logado por chave (`_log_on_change`)
        self._logged_states: dict[str, object] = {}

    def _log_on_change(self, key: str, state: object, msg: str, *args) -> None:
        """Loga (DEBUG) só quando `state` muda: nada de uma linha por tick."""
        if self._logged_states.get(key, _UNSET) != state:
            self._logged_states[key] = state
            self.logger.debug(msg, *args)

    def set_clock(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    def seed(self, seed: int | str | None) -> None:
        self.rng = random.Random(seed)

    @abstractmethod
    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        pass

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        quantity = (balance * Decimal("0.5")) / price
        return quantity

    def setup(self, ticker_history: list[TickerData]):
        return

    def __repr__(self):
        _vars = vars(self)
        hidden = ("logger", "clock", "rng", "_logged_states")
        _vars = {k: str(v) for k, v in vars(self).items() if k not in hidden}
        return f"{self.__class__.__name__} with {_vars}"


class _CappedSizing:
    """Compra `order_usd` quando o saldo cobre; senão `balance_percent` dele.

    Em unidades do token de entrada, que são USD nas entradas stablecoin que
    estas estratégias exigem.
    """

    order_usd: Decimal
    balance_percent: Decimal

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        if balance >= self.order_usd:
            return self.order_usd / price
        return (balance * (self.balance_percent / Decimal("100"))) / price


class RandomStrategy(TradingStrategy):
    def __init__(
        self, sell_chance: int, buy_chance: int, seed: int | str | None = None
    ):
        super().__init__()
        self.buy_chance = buy_chance
        self.sell_chance = sell_chance
        if seed is not None:
            self.seed(seed)

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        quantity = (balance * Decimal("1.0")) / price
        return quantity

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        if not current_position:
            if self.rng.randint(1, 100) <= int(self.buy_chance):
                self.logger.debug("buying at random")
                return OrderSignal(
                    OrderSide.BUY,
                    quantity=self.calculate_quantity(balance, price),
                )
        else:
            if self.rng.randint(1, 100) <= int(self.sell_chance):
                self.logger.debug("selling at random")
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )
        return None


class TargetValueStrategy(_CappedSizing, TradingStrategy):
    """
    Estratégia de valor alvo com stop loss dinâmico.

    O bot compra quando o preço atinge um valor alvo configurado.
    Acompanha o valor até atingir um percentual de ganho configurado.
    Quando atingir esse percentual, ativa um stop loss de 1% (vende se cair 1%).

    Congelada (legado): novas estratégias são specs (`trader/strategy_spec/`).

    Args:
        target_buy_price (Decimal|str): Preço alvo para compra
        target_profit_percent (Decimal|str): Percentual de ganho alvo (ex: 5 para 5%)
        stop_loss_percent (Decimal|str): Percentual de stop loss após atingir ganho alvo (padrão: 1 para 1%)
        balance_percent (Decimal|str): Percentual do saldo a usar na compra (padrão: 80 para 80%)
        order_usd (Decimal|str): Valor por compra quando o saldo cobre (padrão: 5)
    """

    def __init__(
        self,
        target_buy_price: Decimal | str,
        target_profit_percent: Decimal | str,
        stop_loss_percent: Decimal | str = "1",
        balance_percent: Decimal | str = "80",
        max_spread: Decimal | str = "1.5",
        order_usd: Decimal | str = "5",
    ):
        super().__init__()
        self.target_buy_price = Decimal(str(target_buy_price))
        self.target_profit_percent = Decimal(str(target_profit_percent))
        self.stop_loss_percent = Decimal(str(stop_loss_percent))
        self.balance_percent = Decimal(str(balance_percent))
        self.max_spread = Decimal(str(max_spread))
        self.order_usd = Decimal(str(order_usd))

        # Estado interno
        self.target_profit_reached = False
        self.highest_price_after_target = Decimal("0")
        self.last_price = None

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        if current_position:
            signal, decided = self._check_sell(price, current_position)
        else:
            signal, decided = self._check_buy(price, spread, balance)
        if decided:
            return signal
        self.last_price = price
        return None

    def _check_buy(
        self, price: Decimal, spread: Decimal | None, balance: Decimal
    ) -> tuple[OrderSignal | None, bool]:
        """Sem posição. Retorna (sinal, decidido); decidido=False segue o fluxo."""
        # Reset do estado quando não há posição
        self.target_profit_reached = False
        self.highest_price_after_target = Decimal("0")

        # Compra quando o preço atingir ou estiver abaixo do valor alvo
        if price > self.target_buy_price:
            self._log_on_change(
                "buy", "above", "Aguardando alvo de compra %s", self.target_buy_price
            )
            return None, False
        if spread is not None and spread > self.max_spread:
            self._log_on_change("buy", "spread", "Sem compra: spread alto (%s)", spread)
            return None, True
        if self.last_price is None or price < self.last_price:
            self._log_on_change(
                "buy", "dropping", "Sem compra: preço ainda caindo (%s)", price
            )
            self.last_price = price
            return None, True
        self._log_on_change(
            "buy", "buy", "Preço %s <= alvo %s: comprando", price, self.target_buy_price
        )
        return OrderSignal(OrderSide.BUY, self.calculate_quantity(balance, price)), True

    def _check_sell(
        self, price: Decimal, position: Position
    ) -> tuple[OrderSignal | None, bool]:
        """Com posição: trailing stop depois de atingir o lucro alvo."""
        entry_price = position.entry_order.price
        profit_percent = ((price - entry_price) / entry_price) * Decimal("100")

        # Verifica se atingiu o ganho alvo
        in_target_band = profit_percent >= self.target_profit_percent or (
            self.target_profit_reached
            and profit_percent >= self.target_profit_percent - Decimal("1.1")
        )
        if not in_target_band:
            return None, False

        if not self.target_profit_reached:
            # Primeira vez que atinge o ganho alvo
            self.target_profit_reached = True
            self.highest_price_after_target = price
            self.logger.debug("Lucro alvo atingido em %s", price)
        # Atualiza o preço mais alto após atingir o ganho alvo
        self.highest_price_after_target = max(self.highest_price_after_target, price)

        # Calcula a queda percentual desde o pico
        drop_percent = (
            (self.highest_price_after_target - price) / self.highest_price_after_target
        ) * Decimal("100")

        # Ativa stop loss se cair o percentual configurado
        if drop_percent >= self.stop_loss_percent:
            self.logger.debug("Preço %s: vendendo (stop após o alvo)", price)
            return OrderSignal(OrderSide.SELL, position.entry_order.quantity), True
        return None, False


class WeightedMovingAverageStrategy(TradingStrategy):
    """
    Estratégia baseada em médias móveis ponderadas.
    Apenas compra quando a média curta está abaixo da média longa.
    A venda acontece seguindo a estratégia de target value.
    """

    def __init__(
        self,
        short_window: int = 15,
        long_window: int = 200,
        buy_when_short_below: bool = True,
        period: int = 60,
        shift_past: int = 0,
    ):
        super().__init__()
        self.short_window = short_window
        self.long_window = long_window
        self.buy_when_short_below = buy_when_short_below
        self.period = period
        self.shift_past = shift_past

        self.price_history: list[Decimal] = []
        self.last_price_time = datetime.min

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        quantity = (balance * Decimal("0.8")) / price
        return quantity

    def weighted_moving_average(self, prices: list[Decimal], window: int) -> Decimal:
        if self.shift_past > 0:
            prices = prices[: -self.shift_past]

        weights = list(range(1, window + 1))
        weighted_prices = [
            price * Decimal(weight)
            for price, weight in zip(prices[-window:], weights, strict=False)
        ]
        return sum(weighted_prices) / Decimal(sum(weights))

    def set_parameters(self, price: Decimal, timestamp: datetime | None = None):
        history_limit = self.long_window + self.shift_past
        _now: datetime = self.clock() if timestamp is None else timestamp
        if self.last_price_time + timedelta(seconds=self.period) <= _now:
            self.price_history.append(price)
            self.last_price_time = _now
            if len(self.price_history) > history_limit:
                self.price_history.pop(0)

        if self.last_price_time + timedelta(seconds=self.period) > _now:
            # substitui o ultimo preco da lista enquanto o periodo de atualizacao nao chega
            # pra evitar que a media fique defasada
            self.price_history[-1] = price

    def setup(self, ticker_history):
        for ticker in ticker_history:
            self.set_parameters(ticker.last, ticker.timestamp)
        return super().setup(ticker_history)

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        self.set_parameters(price)

        if len(self.price_history) < self.long_window:
            return None

        short_wma = self.weighted_moving_average(self.price_history, self.short_window)
        long_wma = self.weighted_moving_average(self.price_history, self.long_window)
        wants = (
            short_wma < long_wma if self.buy_when_short_below else short_wma > long_wma
        )
        self._log_on_change(
            "wma",
            wants,
            "(S%s L%s %s = %s)",
            self.short_window,
            self.long_window,
            "B" if self.buy_when_short_below else "A",
            "OK" if wants else "NOK",
        )
        if wants and not current_position:
            return OrderSignal(
                OrderSide.BUY,
                quantity=self.calculate_quantity(balance, price),
            )
        return None


class TrailingStopLossStrategy(_CappedSizing, TradingStrategy):
    """
    Estratégia de stop loss dinâmico.
    Acompanha o preço após a compra e ativa um stop loss quando o preço cai um percentual configurado.
    """

    def __init__(
        self,
        stop_loss_percent: Decimal | str = "1",
        balance_percent: Decimal | str = "80",
        order_usd: Decimal | str = "5",
    ):
        super().__init__()
        self.stop_loss_percent = Decimal(str(stop_loss_percent))
        self.balance_percent = Decimal(str(balance_percent))
        self.order_usd = Decimal(str(order_usd))

        # Estado interno
        self.highest_price_after_target = Decimal("0")

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        current_price = price

        # Se não tem posição, verifica se deve comprar
        if not current_position:
            # Essa estratégia não contempla compra. Deixa o composer decidir.
            # Reset do estado quando não há posição
            self.highest_price_after_target = Decimal("0")
            return OrderSignal(
                OrderSide.BUY, quantity=self.calculate_quantity(balance, price)
            )
        else:
            # Atualiza o preço mais alto desde a entrada (o pico começa no
            # preço de entrada, para que a perda máxima respeite o stop)
            self.highest_price_after_target = max(
                self.highest_price_after_target,
                current_position.entry_order.price,
                current_price,
            )

            # Calcula a queda percentual desde o pico
            drop_percent = (
                (self.highest_price_after_target - current_price)
                / self.highest_price_after_target
            ) * Decimal("100")
            # Ativa stop loss se cair o percentual configurado
            if drop_percent >= self.stop_loss_percent:
                self.logger.debug(
                    "Trailing stop: queda de %.2f%% >= %s%%",
                    drop_percent,
                    self.stop_loss_percent,
                )
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )

        return None


class TargetPercentStrategy(_CappedSizing, TradingStrategy):
    """
    Estratégia de Porcentagem de lucro alvo.
    Ao conseguir porcentagem, vende.

    """

    def __init__(
        self,
        target_percent: Decimal | str = "1",
        balance_percent: Decimal | str = "80",
        order_usd: Decimal | str = "5",
    ):
        super().__init__()
        self.target_percent = Decimal(str(target_percent))
        self.balance_percent = Decimal(str(balance_percent))
        self.order_usd = Decimal(str(order_usd))

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        current_price = price

        # Se não tem posição, verifica se deve comprar
        if not current_position:
            # Essa estratégia não contempla compra. Deixa o composer decidir.
            # Reset do estado quando não há posição
            return OrderSignal(
                OrderSide.BUY, quantity=self.calculate_quantity(balance, price)
            )
        else:
            # Calcula o percentual do preco atual em relacão a posicao atual
            current_percent = (
                (current_price - current_position.entry_order.price)
                / current_position.entry_order.price
            ) * Decimal("100")
            # Vende ao atingir o lucro alvo
            if current_percent >= self.target_percent:
                self.logger.debug(
                    "Lucro alvo: %.2f%% >= %s%%", current_percent, self.target_percent
                )
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )

        return None


class StrategyComposer(TradingStrategy):
    """
    Compositor de estratégias de trading.
    Combina múltiplas estratégias e executa caso todas sejam válidas.
    """

    def __init__(
        self,
        sell_mode="all",
        buy_mode="all",
        buy_strategies: list[TradingStrategy] | None = None,
        sell_strategies: list[TradingStrategy] | None = None,
    ):
        super().__init__()
        if sell_mode not in ("all", "any"):
            raise ValueError(f"sell_mode inválido: {sell_mode!r}")
        if buy_mode not in ("all", "any"):
            raise ValueError(f"buy_mode inválido: {buy_mode!r}")
        self.sell_mode = sell_mode
        self.buy_mode = buy_mode
        self.buy_strategies = buy_strategies or []
        if not buy_strategies:
            self.buy_strategies = [
                WeightedMovingAverageStrategy(
                    short_window=50,
                    long_window=100,
                    buy_when_short_below=True,
                    period=15,
                ),
                WeightedMovingAverageStrategy(
                    short_window=15,
                    long_window=100,
                    buy_when_short_below=True,
                    period=15,
                ),
                WeightedMovingAverageStrategy(
                    short_window=15,
                    long_window=30,
                    buy_when_short_below=False,
                    period=15,
                ),
                WeightedMovingAverageStrategy(
                    short_window=5,
                    long_window=10,
                    buy_when_short_below=False,
                    period=15,
                ),
            ]
        self.sell_strategies = sell_strategies or []
        if not sell_strategies:
            self.sell_strategies = [
                TrailingStopLossStrategy(stop_loss_percent="3.1"),
                # TargetPercentStrategy(target_percent="6.01"),
            ]

    def __repr__(self):
        return (
            f"{self.__class__.__name__} "
            f"buy when {self.buy_mode} strategies={', '.join([str(s) for s in self.buy_strategies])}"
            f"sell when {self.sell_mode} strategies={', '.join([str(s) for s in self.sell_strategies])}"
        )

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        # Usa a estratégia principal (primeira da lista) para calcular a quantidade
        return self.buy_strategies[0].calculate_quantity(balance, price)

    def setup(self, ticker_history):
        for strategy in self.buy_strategies + self.sell_strategies:
            strategy.setup(ticker_history)
        return super().setup(ticker_history)

    def set_clock(self, clock: Callable[[], datetime]) -> None:
        super().set_clock(clock)
        for strategy in self.buy_strategies + self.sell_strategies:
            strategy.set_clock(clock)

    def seed(self, seed: int | str | None) -> None:
        super().seed(seed)
        for index, strategy in enumerate(self.buy_strategies + self.sell_strategies):
            strategy.seed(None if seed is None else f"{seed}:{index}")

    def _check_signals(self, signals, mode: str, side: OrderSide) -> bool:
        signal = False
        if mode == "all":
            signal = all(s and s.side == side for s in signals)
        elif mode == "any":
            signal = any(s and s.side == side for s in signals)
        label = str(side) if signal else "HOLD"
        self._log_on_change("signal", label, "[%s]", label)
        return signal

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        if current_position:
            return self._refresh(
                price,
                spread,
                balance,
                current_position,
                self.sell_strategies,
                self.buy_strategies,
                self.sell_mode,
                OrderSide.SELL,
            )
        else:
            return self._refresh(
                price,
                spread,
                balance,
                current_position,
                self.buy_strategies,
                self.sell_strategies,
                self.buy_mode,
                OrderSide.BUY,
            )

    def _refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
        strategies_to_consider,
        strategies_to_ignore,
        mode,
        side,
    ):
        for strategy in strategies_to_ignore:
            # atualiza dados das estrategias, mas ignora signals
            strategy.on_market_refresh(price, spread, balance, current_position)

        signals = []
        for strategy in strategies_to_consider:
            signal = strategy.on_market_refresh(
                price, spread, balance, current_position
            )
            signals.append(signal)

        if self._check_signals(signals, mode, side):
            if side == OrderSide.SELL and current_position:
                # venda encerra a posição inteira; calculate_quantity é
                # dimensionado para compra (saldo do input_mint)
                quantity = current_position.entry_order.quantity
            else:
                quantity = self.calculate_quantity(balance, price)
            return OrderSignal(side, quantity=quantity)
        return None
