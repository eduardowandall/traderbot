import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from trader.models.public_data import TickerData

from .models import OrderSide, OrderSignal, Position


class TradingStrategy(ABC):
    """Classe base para estratégias de trading"""

    def __init__(self):
        self.logger = logging.getLogger(self.__module__)

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
        _vars = {k: str(v) for k, v in vars(self).items() if k != "logger"}
        return f"{self.__class__.__name__} with {_vars}"


class RandomStrategy(TradingStrategy):
    def __init__(self, sell_chance: int, buy_chance: int):
        super().__init__()
        self.buy_chance = buy_chance
        self.sell_chance = sell_chance
        self.price_history: list[Decimal] = []

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
        self.price_history.append(price)
        if not current_position:
            if random.randint(1, 100) <= int(self.buy_chance):
                self.price_history = []
                return OrderSignal(
                    OrderSide.BUY,
                    quantity=self.calculate_quantity(balance, price),
                )
                self.logger.debug("buying at random")
        else:
            if random.randint(1, 100) <= int(self.sell_chance):
                self.price_history = []
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )
                self.logger.debug("selling at random")
        return None


class TargetValueStrategy(TradingStrategy):
    """
    Estratégia de valor alvo com stop loss dinâmico.

    O bot compra quando o preço atinge um valor alvo configurado.
    Acompanha o valor até atingir um percentual de ganho configurado.
    Quando atingir esse percentual, ativa um stop loss de 1% (vende se cair 1%).

    Args:
        target_buy_price (Decimal|str): Preço alvo para compra
        target_profit_percent (Decimal|str): Percentual de ganho alvo (ex: 5 para 5%)
        stop_loss_percent (Decimal|str): Percentual de stop loss após atingir ganho alvo (padrão: 1 para 1%)
        balance_percent (Decimal|str): Percentual do saldo a usar na compra (padrão: 80 para 80%)
    """

    def __init__(
        self,
        target_buy_price: Decimal | str,
        target_profit_percent: Decimal | str,
        stop_loss_percent: Decimal | str = "1",
        balance_percent: Decimal | str = "80",
        max_spread: Decimal | str = "1.5",
    ):
        super().__init__()
        self.target_buy_price = Decimal(str(target_buy_price))
        self.target_profit_percent = Decimal(str(target_profit_percent))
        self.stop_loss_percent = Decimal(str(stop_loss_percent))
        self.balance_percent = Decimal(str(balance_percent))
        self.max_position_periods = 10
        self.max_spread = Decimal(str(max_spread))

        # Estado interno
        self.target_profit_reached = False
        self.highest_price_after_target = Decimal("0")
        self.position_periods = 0
        self.last_price = None
        self.price_history: list[Decimal] = []
        self.max_history_size = 60 * 60 * 4 / 10  # 4h
        self.same_target_count = 0
        self.report_interval = 60 * 60 / 10  # 1h

    def _recalculate_target_buy_price(self):
        """
        Calcula o target buy (preço-alvo de compra) com base em uma lista de preços históricos (Decimal).
        Segue a mesma lógica da função original, mas sem puxar dados de candles da API.
        """
        if len(self.price_history) < 200:
            return

        # -----------------------
        # 1. Converter lista em DataFrame
        # -----------------------
        df = pd.DataFrame({"c": [float(p) for p in self.price_history]})
        df.index = pd.RangeIndex(start=0, stop=len(df))

        # Como não temos high/low, podemos derivar deles:
        # assumindo que o preço oscilou ±0.2% em cada candle
        df["h"] = df["c"] * 1.005
        df["l"] = df["c"] * 0.995

        # -----------------------
        # 2. Calcular EMA50 e EMA200
        # -----------------------
        df["EMA50"] = df["c"].ewm(span=50, adjust=False).mean()
        df["EMA200"] = df["c"].ewm(span=200, adjust=False).mean()

        # -----------------------
        # 3. Calcular ATR (Average True Range)
        # -----------------------
        df["H-L"] = df["h"] - df["l"]
        df["H-PC"] = abs(df["h"] - df["c"].shift(1))
        df["L-PC"] = abs(df["l"] - df["c"].shift(1))
        df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        df["ATR"] = df["TR"].rolling(window=60).mean()

        # -----------------------
        # 4. Calcular RSI14
        # -----------------------
        delta = df["c"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / avg_loss
        df["RSI14"] = 100 - (100 / (1 + rs))

        # -----------------------
        # 5. Calcular Target Buy
        # -----------------------
        latest = df.iloc[-1]
        k = 1.5 if latest["EMA50"] > latest["EMA200"] else 2.2
        target_buy = latest["EMA50"] - k * latest["ATR"]

        # -----------------------
        # 6. Exibir resultados
        # -----------------------
        self.logger.info(f"Último preço: {latest['c']:.8f}")
        self.logger.info(f"EMA50: {latest['EMA50']:.8f}")
        self.logger.info(f"EMA200: {latest['EMA200']:.8f}")
        self.logger.info(f"ATR: {latest['ATR']:.8f}")
        self.logger.info(f"RSI14: {latest['RSI14']:.2f}")
        self.logger.info(f"Target buy calculado: {target_buy:.8f}")

        self.target_buy_price = target_buy

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        """Calcula a quantidade a comprar baseado no saldo disponível"""
        if balance >= Decimal("5"):
            return Decimal("5") / price
        quantity = (balance * (self.balance_percent / Decimal("100"))) / price
        return quantity

    def on_market_refresh(
        self,
        price: Decimal,
        spread: Decimal | None,
        balance: Decimal,
        current_position: Position | None,
    ) -> OrderSignal | None:
        current_price = price

        self.price_history.append(current_price)
        self.same_target_count += 1
        if len(self.price_history) > self.max_history_size:
            self.price_history.pop(0)

        # Se não tem posição, verifica se deve comprar
        if not current_position:
            # if self.same_target_count > self.max_history_size:
            #     self._recalculate_target_buy_price()
            #     self.same_target_count = 0
            # Reset do estado quando não há posição
            self.target_profit_reached = False
            self.highest_price_after_target = Decimal("0")

            # Compra quando o preço atingir ou estiver abaixo do valor alvo
            if current_price <= self.target_buy_price:
                if spread is not None and spread > self.max_spread:
                    self.logger.info(f"Skip buying for high spread = {spread}")
                    self.logger.info(
                        f"Current price: {current_price}; target buy: {self.target_buy_price}; spread: {spread}"
                    )
                    return None
                if self.last_price is None or current_price < self.last_price:
                    self.logger.info("Skip buying - waiting for price to stop dropping")
                    self.logger.info(
                        f"Current price: {current_price}; target buy: {self.target_buy_price}; spread: {spread}"
                    )
                    self.last_price = current_price
                    return None
                self.position_periods = 0
                self.logger.info(
                    f"Current price: {current_price} <= Target buy: {self.target_buy_price} - BUYING!"
                )
                return OrderSignal(
                    OrderSide.BUY,
                    quantity=self.calculate_quantity(balance, price),
                )
        else:
            # Tem posição aberta, verifica condições de venda
            entry_price = current_position.entry_order.price

            # Calcula o percentual de ganho atual
            profit_percent = ((current_price - entry_price) / entry_price) * Decimal(
                "100"
            )

            # Verifica se atingiu o ganho alvo
            if profit_percent >= self.target_profit_percent or (
                self.target_profit_reached
                and profit_percent >= self.target_profit_percent - Decimal("1.1")
            ):
                self.position_periods += 1
                if not self.target_profit_reached:
                    # Primeira vez que atinge o ganho alvo
                    self.target_profit_reached = True
                    self.highest_price_after_target = current_price

                # Atualiza o preço mais alto após atingir o ganho alvo
                if current_price > self.highest_price_after_target:
                    self.highest_price_after_target = current_price

                # Calcula a queda percentual desde o pico
                drop_percent = (
                    (self.highest_price_after_target - current_price)
                    / self.highest_price_after_target
                ) * Decimal("100")

                # if self.position_periods >= self.max_position_periods:
                #     self.logger.info(f"Current price: {current_price} - SELLING!")
                #     return OrderSignal(
                #         OrderSide.SELL, current_position.entry_order.quantity
                #     )

                # Ativa stop loss se cair o percentual configurado
                if drop_percent >= self.stop_loss_percent:
                    self.logger.info(f"Current price: {current_price} - SELLING!")
                    return OrderSignal(
                        OrderSide.SELL, current_position.entry_order.quantity
                    )

        msg = f"Current price: {current_price:.9f}; target buy: {self.target_buy_price:.9f}"
        self.logger.info(msg)
        self.last_price = current_price
        return None


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
            price * Decimal(weight) for price, weight in zip(prices[-window:], weights)
        ]
        return sum(weighted_prices) / Decimal(sum(weights))

    def set_parameters(self, price: Decimal, timestamp: datetime | None = None):
        history_limit = self.long_window + self.shift_past
        _now: datetime = datetime.now() if timestamp is None else timestamp
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
        msg = f"(S{self.short_window} L{self.long_window} {'B' if self.buy_when_short_below else 'A'} = "
        if not current_position:
            if self.buy_when_short_below and short_wma < long_wma:
                self.logger.info(msg + "OK)")
                return OrderSignal(
                    OrderSide.BUY,
                    quantity=self.calculate_quantity(balance, price),
                )

            if not self.buy_when_short_below and short_wma > long_wma:
                self.logger.info(msg + "OK)")
                return OrderSignal(
                    OrderSide.BUY,
                    quantity=self.calculate_quantity(balance, price),
                )
        self.logger.info(msg + "NOK)")
        return None


class TrailingStopLossStrategy(TradingStrategy):
    """
    Estratégia de stop loss dinâmico.
    Acompanha o preço após a compra e ativa um stop loss quando o preço cai um percentual configurado.
    """

    def __init__(
        self,
        stop_loss_percent: Decimal | str = "1",
        balance_percent: Decimal | str = "80",
    ):
        super().__init__()
        self.stop_loss_percent = Decimal(str(stop_loss_percent))
        self.balance_percent = Decimal(str(balance_percent))

        # Estado interno
        self.highest_price_after_target = Decimal("0")

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        """Calcula a quantidade a comprar baseado no saldo disponível"""
        if balance >= Decimal("5"):
            return Decimal("5") / price
        quantity = (balance * (self.balance_percent / Decimal("100"))) / price
        return quantity

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
            # Atualiza o preço mais alto após atingir o ganho alvo
            if current_price > self.highest_price_after_target:
                self.highest_price_after_target = current_price

            # Calcula a queda percentual desde o pico
            drop_percent = (
                (self.highest_price_after_target - current_price)
                / self.highest_price_after_target
            ) * Decimal("100")
            self.logger.info(
                f"Trail SL: P{drop_percent * -1:.2f}%  SL{self.stop_loss_percent * -1:.2f}%"
            )
            # Ativa stop loss se cair o percentual configurado
            if drop_percent >= self.stop_loss_percent:
                return OrderSignal(
                    OrderSide.SELL, current_position.entry_order.quantity
                )

        return None


class TargetPercentStrategy(TradingStrategy):
    """
    Estratégia de Porcentagem de lucro alvo.
    Ao conseguir porcentagem, vende.

    """

    def __init__(
        self,
        target_percent: Decimal | str = "1",
        balance_percent: Decimal | str = "80",
    ):
        super().__init__()
        self.target_percent = Decimal(str(target_percent))
        self.balance_percent = Decimal(str(balance_percent))

        # Estado interno

    def calculate_quantity(self, balance: Decimal, price: Decimal) -> Decimal:
        """Calcula a quantidade a comprar baseado no saldo disponível"""
        if balance >= Decimal("5"):
            return Decimal("5") / price
        quantity = (balance * (self.balance_percent / Decimal("100"))) / price
        return quantity

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
            self.logger.info(
                f"Target: P{current_percent:.2f}%  T{self.target_percent:.2f}%"
            )

            # Ativa stop loss se cair o percentual configurado
            if current_percent >= self.target_percent:
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
        assert sell_mode in ("all", "any")
        assert buy_mode in ("all", "any")
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
                TargetPercentStrategy(target_percent="6.01"),
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

    def _check_signals(self, signals, mode: str, side: OrderSide) -> bool:
        signal = False
        if mode == "all":
            signal = all(s and s.side == side for s in signals)
        elif mode == "any":
            signal = any(s and s.side == side for s in signals)
        self.logger.info(f"[{str(side) if signal else 'HOLD'}]")
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
            return OrderSignal(
                side,
                quantity=self.calculate_quantity(balance, price),
            )
        return None
