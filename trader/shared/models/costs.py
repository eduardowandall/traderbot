"""Custos reais de cada swap e o PnL líquido deles.

Unidades nativas são a fonte da verdade:
- o PnL bruto de um trade é medido no **token de cotação** do par (o token
  gasto na compra e recebido na venda, ex: USDC em SOL-USDC), a partir dos
  valores efetivamente movidos on-chain;
- os custos da transação são pagos em **SOL** (taxa de rede, priority fee,
  rent de contas criadas e outros débitos da rota).

Taxas de LP/AMM, impacto de preço e slippage já estão embutidos nos valores
efetivos: são registrados só como informação e **nunca** subtraídos de novo.

Os valores em USD são estimativas, com taxas derivadas do próprio trade (o
feed dá o preço do token em USD e o fill dá cotação por token).
"""

from dataclasses import dataclass, field
from decimal import Decimal

LAMPORTS_PER_SOL = Decimal(10**9)
BASE_FEE_LAMPORTS = 5000  # por assinatura
# teto padrão da priority fee por transação (0.0001 SOL); o dono muda em
# `policy.toml` (`max_priority_fee_lamports`)
DEFAULT_MAX_PRIORITY_FEE_LAMPORTS = 100_000

# origem dos custos
ONCHAIN = "onchain"  # lidos da transação confirmada
SIMULATED = "simulated"  # paper trading
REPLAY = "replay"  # backtest (custos modelados em fee_bps)
QUOTE = "quote"  # não foi possível obter: valores da quote, custos desconhecidos


@dataclass(frozen=True)
class TradeCosts:
    source: str
    fee_lamports: int = 0  # taxa de rede total (base + priority)
    priority_fee_lamports: int = 0
    rent_lamports: int = 0  # contas de token abertas (negativo = reembolso)
    other_lamports: int = 0  # outros débitos em SOL da rota (contas, gorjetas)
    # valores raw efetivamente movidos; None = usa a quote
    actual_in_amount: int | None = None
    actual_out_amount: int | None = None
    quoted_out_amount: int | None = None
    # informativos (já embutidos nos valores efetivos)
    lp_fees: dict[str, int] = field(default_factory=dict)
    price_impact_pct: str | None = None

    @property
    def known(self) -> bool:
        """False quando os custos reais não puderam ser obtidos."""
        return self.source != QUOTE

    @property
    def native_cost_lamports(self) -> int:
        return self.fee_lamports + self.rent_lamports + self.other_lamports

    @property
    def native_cost_sol(self) -> Decimal:
        return Decimal(self.native_cost_lamports) / LAMPORTS_PER_SOL

    @property
    def slippage_raw(self) -> int | None:
        """Quanto a menos (raw) se recebeu em relação à quote."""
        if self.quoted_out_amount is None or self.actual_out_amount is None:
            return None
        return self.quoted_out_amount - self.actual_out_amount


@dataclass(frozen=True)
class FailedTxFee:
    """Taxas de transações que a rede confirmou como falhas: custo sem trade."""

    signatures: tuple[str, ...]
    lamports: int
    usd: Decimal | None  # None: sem preço do SOL

    @property
    def sol(self) -> Decimal:
        return Decimal(self.lamports) / LAMPORTS_PER_SOL


def costs_from_dict(data: dict | None) -> TradeCosts | None:
    return TradeCosts(**data) if data else None


@dataclass(frozen=True)
class TradeRates:
    quote_usd: Decimal | None  # USD por unidade do token de cotação
    sol_usd: Decimal | None  # USD por SOL
    sol_in_quote: Decimal | None  # unidades do token de cotação por SOL


def trade_rates(
    price_usd: Decimal,
    fill_price: Decimal,
    quote_is_usd: bool,
    quote_is_sol: bool,
    token_is_sol: bool,
) -> TradeRates:
    """Taxas de conversão tiradas do próprio trade.

    price_usd: preço de mercado do token em USD (feed); fill_price: unidades
    do token de cotação por token, no fill.
    """
    if quote_is_usd:
        quote_usd: Decimal | None = Decimal("1")
    else:
        quote_usd = price_usd / fill_price if fill_price else None
    if quote_is_sol:
        return TradeRates(quote_usd, quote_usd, Decimal("1"))
    if token_is_sol:
        return TradeRates(quote_usd, price_usd, fill_price)
    return TradeRates(quote_usd, None, None)


def with_sol_usd(rates: TradeRates, sol_usd: Decimal | None) -> TradeRates:
    """Completa o preço do SOL de um par sem SOL (vindo da Price API).

    As taxas do próprio trade vêm primeiro; isto só preenche o que falta,
    para os custos (pagos em SOL) terem valor em USD e em cotação.
    """
    if rates.sol_usd is not None or sol_usd is None:
        return rates
    sol_in_quote = sol_usd / rates.quote_usd if rates.quote_usd else None
    return TradeRates(rates.quote_usd, sol_usd, sol_in_quote)


@dataclass(frozen=True)
class PnLResult:
    """PnL de uma posição fechada: nativo (token de cotação) + estimativa USD."""

    quote_symbol: str
    gross_quote: Decimal
    costs_sol: Decimal
    costs_quote: Decimal | None  # None: custos em SOL sem conversão
    gross_usd: Decimal | None
    costs_usd: Decimal | None
    complete: bool  # custos reais conhecidos e convertidos

    @property
    def net_quote(self) -> Decimal | None:
        if self.costs_quote is None:
            return None
        return self.gross_quote - self.costs_quote

    @property
    def net_usd(self) -> Decimal | None:
        """Estimativa USD; quando incompleta, desconta só os custos conhecidos."""
        if self.gross_usd is None:
            return None
        return self.gross_usd - (self.costs_usd or Decimal("0"))

    def summary(self) -> str:
        net = self.net_quote
        net_text = f"{net:+.6f} {self.quote_symbol}" if net is not None else "líquido ?"
        usd = self.net_usd
        usd_text = f" (~${usd:+.4f})" if usd is not None else ""
        flag = "" if self.complete else " [!] incompleto"
        return (
            f"PNL líquido {net_text}{usd_text}; bruto "
            f"{self.gross_quote:+.6f} {self.quote_symbol}, custos "
            f"{self.costs_sol:.9f} SOL{flag}"
        )


def sol_text(lamports: int) -> str:
    return f"{Decimal(lamports) / LAMPORTS_PER_SOL:.9f}"


def describe_costs(costs: TradeCosts | None, sol_usd: Decimal | None = None) -> str:
    """Linha legível com os custos de uma perna."""
    if costs is None or not costs.known:
        return "custos: desconhecidos (valores da quote)"
    usd = f" (~${costs.native_cost_sol * sol_usd:.4f})" if sol_usd else ""
    text = (
        f"custos [{costs.source}]: taxa {sol_text(costs.fee_lamports)} SOL "
        f"(priority {sol_text(costs.priority_fee_lamports)}), "
        f"rent {sol_text(costs.rent_lamports)} SOL, "
        f"outros {sol_text(costs.other_lamports)} SOL{usd}"
    )
    if costs.slippage_raw:
        text += f"; slippage {costs.slippage_raw} raw"
    return text


BPS = Decimal(10_000)


@dataclass
class RoundTripCosts:
    """Quanto as idas e voltas fechadas custaram, tudo somado.

    Por perna de venda: o movimento que um trade sem custo teria feito com o
    que a entrada gastou (gasto x (preço do tick na saída / preço do tick na
    entrada - 1)) menos o PnL líquido realizado. Sobra spread, taxas de pool,
    slippage e taxas de rede (base + priority) num número só, igual no real,
    no paper e no backtest (todos gravam o preço do tick na intenção).
    """

    count: int = 0  # posições fechadas
    notional_usd: Decimal = Decimal("0")  # gasto na entrada das pernas somadas
    cost_usd: Decimal = Decimal("0")

    def add(self, cost_usd: Decimal, notional_usd: Decimal, closes: bool) -> None:
        self.cost_usd += cost_usd
        self.notional_usd += notional_usd
        self.count += closes

    def refund(self, net_usd: Decimal) -> None:
        """Rent devolvido (menos a taxa do fechamento): sai do custo (A15)."""
        self.cost_usd -= net_usd

    @property
    def per_trip_usd(self) -> Decimal | None:
        return self.cost_usd / self.count if self.count else None

    @property
    def bps(self) -> Decimal | None:
        """Em bps do que as entradas gastaram (pesado pelo valor)."""
        return self.cost_usd / self.notional_usd * BPS if self.notional_usd else None

    def describe(self) -> str:
        if self.per_trip_usd is None or self.bps is None:
            return "custo por ida e volta: sem idas e voltas fechadas"
        return (
            f"custo por ida e volta ~${self.per_trip_usd:.4f} "
            f"({self.bps:.1f} bps) em {self.count}"
        )

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "notional_usd": self.notional_usd,
            "cost_usd": self.cost_usd,
            "per_trip_usd": self.per_trip_usd,
            "bps": self.bps,
        }
