"""`LocalTradeClient`: `TradeClient` para um bucket de um `TradeService` no
mesmo processo (testes, backtest e o `main.py run` de sempre)."""

from decimal import Decimal

from trader.trading_service.protocol import BucketSnapshot, OrderReply, OrderRequest
from trader.trading_service.service import TradeService


class LocalTradeClient:
    def __init__(
        self,
        service: TradeService,
        name: str,
        input_mint: str,
        output_mint: str,
        budget_usd: Decimal | None = None,
        source: str = "strategy",
        # quem cria o serviço só para este cliente passa True: fecha junto
        owns_service: bool = False,
    ):
        self.service = service
        self.name = name
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.budget_usd = budget_usd
        self.source = source
        self.owns_service = owns_service

    async def open(self) -> None:
        await self.service.open_bucket(
            self.name, self.input_mint, self.output_mint, self.budget_usd, self.source
        )

    async def bucket(self) -> BucketSnapshot:
        return await self.service.get_bucket(self.name)

    async def submit(self, request: OrderRequest) -> OrderReply:
        return await self.service.submit_order(self.name, request)

    async def aclose(self) -> None:
        if self.owns_service:
            await self.service.aclose()
