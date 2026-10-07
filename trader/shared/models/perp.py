"""Uma perna de perp como ela foi executada (A8, D9).

Vai no `ExecutionResult` do venue e no `Order` (e por ele no ledger, no fio e
no `Position`): a ordem de entrada guarda o lado, e a posição o lê dali
(restore incluído). Convenção da ordem de uma perna de perp: `quantity` é o
tamanho no token base, `fill_price` o preço do oráculo e `quote_amount` o
colateral postado (entrada) ou devolvido (saída); o PnL de sempre vira
colateral devolvido - colateral postado.

As taxas são as da Jupiter Perps (§8.2 do plano): 0.06% do tamanho em cada
ponta, o empréstimo por hora sobre o tamanho e a liquidação na margem de
manutenção (0.2% do tamanho).
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from trader.shared.models.direction import Direction

# 0.06% do tamanho na abertura e no fechamento
PERP_FEE_RATE = Decimal("0.0006")
# margem de manutenção: tamanho / 500
MAINTENANCE_RATE = Decimal("0.002")
HOUR_SECONDS = Decimal(3600)
# os padrões do dono (política e limites da spec, D10)
DEFAULT_MAX_LEVERAGE = Decimal(3)
DEFAULT_PERP_MARKETS = ("SOL",)
BPS = Decimal(10_000)
ZERO = Decimal(0)


@dataclass(frozen=True)
class PerpFill:
    direction: Direction
    leverage: Decimal
    price: Decimal  # do oráculo, no token de cotação (USD numa stablecoin)
    size_usd: Decimal  # a exposição
    # entrada: o colateral que ficou na posição (depois das taxas de abertura);
    # saída: o que voltou para a carteira
    collateral_usd: Decimal
    fees_usd: Decimal  # taxa + impacto desta perna
    borrow_bps_hour: Decimal  # a taxa de empréstimo da posição
    borrow_usd: Decimal = ZERO  # pago no fechamento
    liquidation_price: Decimal | None = None  # da entrada
    liquidated: bool = False

    def pnl_usd(self, price: Decimal) -> Decimal:
        """O ganho do preço até `price`, no tamanho e no lado da posição."""
        return self.direction.sign * (price - self.price) / self.price * self.size_usd

    def borrow_at(self, opened_at: datetime, now: datetime) -> Decimal:
        """O empréstimo acumulado de `opened_at` até `now`."""
        hours = Decimal(max((now - opened_at).total_seconds(), 0)) / HOUR_SECONDS
        return self.size_usd * self.borrow_bps_hour / BPS * hours

    def close_fee(self) -> Decimal:
        """A taxa de fechamento sem o impacto (0.06% do tamanho)."""
        return self.size_usd * PERP_FEE_RATE

    def equity(
        self, price: Decimal, borrow: Decimal = ZERO, close_fee: Decimal | None = None
    ) -> Decimal:
        """O que um fechamento a `price` devolveria.

        `close_fee`: a do venue (com o impacto); sem ela, só os 0.06%.
        """
        fee = self.close_fee() if close_fee is None else close_fee
        return self.collateral_usd + self.pnl_usd(price) - borrow - fee


def liquidation_price(fill: PerpFill, close_fee: Decimal | None = None) -> Decimal:
    """O preço em que `equity` chega à margem de manutenção (§8.2)."""
    cushion = fill.equity(fill.price, close_fee=close_fee)
    cushion -= fill.size_usd * MAINTENANCE_RATE
    return fill.price * (1 - fill.direction.sign * cushion / fill.size_usd)


def _liquidation_text(fill: PerpFill) -> str:
    price = fill.liquidation_price
    return "" if price is None else f", liquidação {price:.4f}"


def describe_fill(fill: PerpFill) -> str:
    """Uma perna de perp numa linha (avisos de fill)."""
    text = (
        f"perp {fill.direction} {fill.leverage}x @ {fill.price:.4f}: tamanho "
        f"${fill.size_usd:.2f}, colateral ${fill.collateral_usd:.4f}, taxas "
        f"${fill.fees_usd:.4f}"
    )
    if fill.borrow_usd:
        text += f", empréstimo ${fill.borrow_usd:.4f}"
    text += _liquidation_text(fill)
    return text + (" [!] LIQUIDADA" if fill.liquidated else "")


def describe_open(fill: PerpFill, borrow: Decimal) -> str:
    """Uma posição aberta: lado, alavancagem, liquidação e o empréstimo até agora."""
    return (
        f"{fill.direction} {fill.leverage}x{_liquidation_text(fill)}, "
        f"empréstimo ~${borrow:.4f}"
    )


def perp_to_dict(fill: PerpFill | None) -> dict | None:
    """Para JSON: decimais como texto (exatos), o resto como é."""
    if fill is None:
        return None
    return {
        k: str(v) if isinstance(v, Decimal | Direction) else v
        for k, v in asdict(fill).items()
    }


def perp_from_dict(data: dict | None) -> PerpFill | None:
    if data is None:
        return None
    price = data.get("liquidation_price")
    return PerpFill(
        direction=Direction(data["direction"]),
        leverage=Decimal(data["leverage"]),
        price=Decimal(data["price"]),
        size_usd=Decimal(data["size_usd"]),
        collateral_usd=Decimal(data["collateral_usd"]),
        fees_usd=Decimal(data["fees_usd"]),
        borrow_bps_hour=Decimal(data["borrow_bps_hour"]),
        borrow_usd=Decimal(data.get("borrow_usd", "0")),
        liquidation_price=None if price is None else Decimal(price),
        liquidated=bool(data.get("liquidated", False)),
    )
