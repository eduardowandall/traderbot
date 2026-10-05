"""`trader/execution/wiring.py`: o modo vira componentes com a política do modo."""

from trader.execution.models.mode import RunningMode
from trader.execution.trade.venues.paper.executor import SimulatedExecutor
from trader.execution.wiring import build_trade_service
from trader.shared.paths import policy_file


async def test_paper_charges_the_priority_fee_cap_of_its_policy():
    policy_file().write_text(
        "[paper.trading]\nmax_priority_fee_lamports = 42000\n", encoding="utf-8"
    )
    service = build_trade_service(RunningMode.PAPER)
    with service.gateway:
        executor = service.provider.executor
        assert isinstance(executor, SimulatedExecutor)
        assert executor.priority_fee_lamports == 42_000
        # a mesma política no gateway (carregada uma vez)
        assert service.gateway.policy.max_priority_fee_lamports == 42_000
        await service.aclose()


async def test_the_process_oracle_prices_the_service_and_the_quote_check():
    class Oracle:
        async def usd_prices(self, mints):
            return {}

    oracle = Oracle()
    service = build_trade_service(RunningMode.PAPER, prices=oracle)
    with service.gateway:
        assert service.prices is oracle
        assert service.provider.usd_prices == oracle.usd_prices
        await service.aclose()
