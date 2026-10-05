"""B3: trade-runner, strategy-runner, o protocolo e o lock por modo."""

import asyncio
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import (
    NoCandles,
    PriceTable,
    make_intent,
    make_spec,
    open_ledger,
    terms_of,
    trade_runner,
)
from typer.testing import CliRunner

import main as main_module
from trader.api.cli import runners as cli_runners
from trader.api.cli.lock import ModeBusyError, ModeLock
from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.models.intent import IntentStatus
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.shared.market import HubMarketData
from trader.shared.models import SOLANA_MINTS, Order, OrderSide, Position
from trader.shared.models.costs import TradeCosts
from trader.shared.models.public_data import Interval, TickerData
from trader.shared.paths import PROJECT_ROOT, connection_path, data_dir
from trader.shared.spec.validate import SpecLimits
from trader.shared.trading_service import wire
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    OrderRequest,
    ReplyStatus,
    TradeServiceError,
)
from trader.strategy.runner import find_connection, strategy_bot
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.strategy import SpecStrategy
from trader.strategy.trading_service.remote import (
    HelloRefusedError,
    RemoteCandles,
    RemoteTradeClient,
)

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
)
SPEC = make_spec(ttl_days=5, expires_at=None)


def _order(side=OrderSide.BUY):
    costs = TradeCosts("simulated", fee_lamports=5000, actual_in_amount=1)
    return Order(
        "sig",
        USDC.mint,
        SOL.mint,
        Decimal("0.2"),
        Decimal("100"),
        side,
        T0,
        quote_amount=Decimal("20"),
        costs=costs,
    )


# --- o protocolo -----------------------------------------------------------------


def test_the_wire_codec_round_trips():
    snapshot = BucketSnapshot(
        "strategy:x",
        Decimal("30"),
        Position(_order(), None),
        Decimal("-1.5"),
        Decimal(1),
        budget_usd=Decimal(50),
        status=BucketStatus.RETIRING,
        pnl_summary="pnl",
        opened_at=T0,
        last_exit_at=None,
        last_exit_price=Decimal("99.5"),
    )
    request = OrderRequest(OrderSide.SELL, Decimal("0.2"), Decimal("101"), "r", "k")
    reply = OrderReply.of_fill(_order(OrderSide.SELL))

    def through(data):
        return wire.decode(wire.encode(data))

    assert wire.snapshot_from_dict(through(wire.snapshot_to_dict(snapshot))) == snapshot
    assert wire.request_from_dict(through(wire.request_to_dict(request))) == request
    assert wire.reply_from_dict(through(wire.reply_to_dict(reply))) == reply


# --- trade-runner + cliente remoto --------------------------------------------------


def _runner(price="100", ledger=None, limits=None, candles=None):
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(T0, Decimal(price))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    gateway = TradeGateway(ledger or open_ledger(), LOOSE, False)
    service = TradeService(
        paper_provider(wallet, jupiter_client=quotes), gateway, mode="paper"
    )
    # o hub responde pela Price API falsa (USDC vale 1 sem perguntar)
    return trade_runner(service, limits, {SOL.mint: Decimal(price)}, candles)


async def _client(runner, server, spec=SPEC, token=None, **kwargs):
    port = server.sockets[0].getsockname()[1]
    client = RemoteTradeClient(
        "127.0.0.1",
        port,
        token or runner.token,
        terms_of(spec),
        backoff_initial=0.01,
        **kwargs,
    )
    await client.open()
    return client


def _buy(key=None):
    return OrderRequest(
        OrderSide.BUY, Decimal("0.2"), Decimal("100"), idempotency_key=key
    )


async def test_a_strategy_runner_trades_through_the_socket():
    runner = _runner()
    async with await runner.start() as server:
        client = await _client(runner, server)
        snapshot = await client.bucket()
        assert snapshot.bucket == client.bucket_name
        assert snapshot.available == Decimal(50)  # budget_usd da spec

        reply = await client.submit(_buy())
        assert reply.status == ReplyStatus.FILLED and reply.order is not None
        position = (await client.bucket()).position
        assert position is not None and position.entry_order.quantity == Decimal("0.2")
        (record,) = runner.service.gateway.ledger.list_intents()
        assert record.intent.account == f"paper:{client.bucket_name}"
        assert record.intent.idempotency_key  # o cliente sempre manda uma chave
        await client.aclose()


async def test_hello_refusals():
    runner = _runner(limits=SpecLimits(Decimal(10)))  # sizing da spec: 20 USD
    async with await runner.start() as server:
        with pytest.raises(HelloRefusedError, match="token"):
            await _client(runner, server, token="errado")
        with pytest.raises(HelloRefusedError, match="sizing"):
            await _client(runner, server)
        assert "termos inválidos" in await _raw_hello(runner, server, {"spec_id": 1})


async def _raw_hello(runner, server, terms) -> str:
    """Um `hello` cru (termos malformados não passam pelo `SpecTerms`)."""
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        hello = {"op": "hello", "token": runner.token, "terms": terms}
        writer.write(wire.encode(hello))
        answer = wire.decode(await reader.readline())
    finally:
        writer.close()
    assert not answer["ok"]
    return answer["error"]


async def test_one_live_connection_per_spec():
    runner = _runner()
    async with await runner.start() as server:
        first = await _client(runner, server)
        with pytest.raises(HelloRefusedError, match="já está conectada"):
            await _client(runner, server)
        await first.aclose()
        await first.bucket()  # reconecta sozinho: a spec voltou a ficar livre
        assert first.bucket_name in runner.live
        await first.aclose()


async def _until(predicate, timeout=2.0):
    """Espera o servidor processar algo (ex: uma conexão que caiu)."""
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condição não ocorreu a tempo")


async def test_the_log_shows_bucket_opens_connects_and_disconnects(caplog):
    caplog.set_level(logging.WARNING, logger="trader.execution.runner")
    runner = _runner()
    async with await runner.start() as server:
        client = await _client(runner, server)
        await client.aclose()
        await _until(lambda: not runner.live)
        await client.bucket()  # reconecta: o bucket já está aberto
        await client.aclose()
        await _until(lambda: not runner.live)

    bucket = client.bucket_name or ""
    label = f"sol-dip ({bucket.removeprefix('strategy:')})"
    connected = f"Spec {label} conectada ao bucket {bucket}"
    assert [
        r.getMessage()
        for r in caplog.records
        if r.name == "trader.execution.runner" and "Trade-runner" not in r.msg
    ] == [
        f"Bucket {bucket} aberto para a spec sol-dip: SOL-USDC, orçamento 50 USD, "
        "perda máxima 10 USD",
        f"{connected} (1 conectada(s))",
        f"Spec {label} desconectada (0 conectada(s))",
        f"{connected} (1 conectada(s))",  # reconexão: o bucket não reabre
        f"Spec {label} desconectada (0 conectada(s))",
    ]


async def test_a_resend_after_a_dropped_connection_executes_once():
    runner = _runner()
    async with await runner.start() as server:
        client = await _client(runner, server)
        await client.aclose()  # a conexão cai antes do pedido
        assert (await client.submit(_buy(key="k"))).filled  # reconectou e enviou
        # o mesmo pedido de novo (ex: a resposta se perdeu): não executa outra vez
        again = await client.submit(_buy(key="k"))
        assert not again.filled
        executed = [
            r
            for r in runner.service.gateway.ledger.list_intents()
            if r.status == IntentStatus.EXECUTED
        ]
        assert len(executed) == 1
        await client.aclose()


async def test_the_sweep_sells_a_retired_or_expired_leftover_once():
    runner = _runner()
    async with await runner.start() as server:
        client = await _client(runner, server)
        assert (await client.submit(_buy())).filled
        await client.aclose()  # o strategy-runner morreu com a posição aberta

        await runner.sweep()  # ativa e dentro da validade: nada
        assert (await runner.service.get_bucket(client.bucket_name or "")).position

        later = datetime.now(UTC) + timedelta(days=6)  # ttl_days = 5
        await runner.sweep(now=later)
        await runner.sweep(now=later)
        snapshot = await runner.service.get_bucket(client.bucket_name or "")
        assert snapshot.status == BucketStatus.RETIRING and snapshot.position is None
        sells = [
            r
            for r in runner.service.gateway.ledger.list_intents()
            if r.intent.side == "sell"
        ]
        assert len(sells) == 1


async def test_an_error_reply_is_raised_on_the_strategy_side():
    runner = _runner()
    async with await runner.start() as server:
        client = await _client(runner, server)
        with pytest.raises(TradeServiceError, match="desconhecida"):
            await client._call({"op": "nope"})
        await client.aclose()


# --- o lock por modo e o CLI -----------------------------------------------------


async def test_the_same_key_never_executes_twice_even_on_another_bucket():
    # a chave de idempotência vale no ledger inteiro, não só no bucket
    runner = _runner()
    async with await runner.start() as server:
        first = await _client(runner, server)
        other = await _client(runner, server, spec={**SPEC, "budget_usd": 40})
        try:
            assert (await first.submit(_buy(key="k"))).filled
            again = await other.submit(_buy(key="k"))
            assert again.status == ReplyStatus.DENIED
            assert "duplicada" in again.reasons[0]
        finally:
            await first.aclose()
            await other.aclose()


def test_the_mode_lock_refuses_a_second_process():
    with ModeLock("paper"):
        code = (
            "import sys\n"
            "from trader.api.cli.lock import ModeLock, ModeBusyError\n"
            "try:\n"
            "    ModeLock('paper').acquire()\n"
            "except ModeBusyError:\n"
            "    sys.exit(3)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT)
        assert proc.returncode == 3
        with pytest.raises(ModeBusyError):
            ModeLock("paper").acquire()
    with ModeLock("paper"):  # solto ao sair
        pass


def test_serve_refuses_to_start_while_the_mode_is_busy():
    with ModeLock("paper"):
        result = CliRunner().invoke(main_module.app, ["serve", "paper"])
    assert result.exit_code == 1
    assert "já está rodando" in result.stderr


async def test_hello_opens_the_bucket_from_the_terms():
    # B13: o bucket nasce dos termos (orçamento, perda máxima, prazo)
    runner = _runner()
    spec = make_spec(expires_at=None, ttl_days=7)
    async with await runner.start() as server:
        client = await _client(runner, server, spec=spec)
        snapshot = await client.bucket()
        await client.aclose()
    assert snapshot.bucket == f"strategy:{terms_of(spec).spec_id}"
    assert snapshot.budget_usd == Decimal(50)
    assert runner.specs[snapshot.bucket] == terms_of(spec)  # perda máxima e prazo


async def test_hello_enforces_the_policys_expiry_window():
    runner = _runner()
    async with await runner.start() as server:
        with pytest.raises(HelloRefusedError, match="mais de 30 dias"):
            await _client(runner, server, spec=make_spec())  # expira em 2099


def test_connect_needs_no_key_and_finds_the_trade_runner(monkeypatch, tmp_path):
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    with pytest.raises(ValueError, match="nenhum"):
        find_connection()
    data_dir().mkdir(parents=True, exist_ok=True)
    info = {"host": "127.0.0.1", "port": 1, "token": "t", "pid": 1}
    (connection_path("paper")).write_text(json.dumps(info), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")

    with mock.patch("trader.strategy.runner.AsyncWebsocketTradingBot") as bot:
        result = CliRunner().invoke(main_module.app, ["connect", str(spec)])

    assert result.exit_code == 0, result.output
    config = bot.call_args.args[0]
    assert isinstance(config.trader, RemoteTradeClient)
    assert (config.trader.port, config.trader.token) == (1, "t")
    bot.return_value.run.assert_called_once()


async def test_serve_runs_the_daily_report_beside_the_server_and_closes_all():
    runner = mock.Mock()
    runner.serve = AsyncMock(return_value=None)  # o servidor parou
    service = mock.Mock()
    service.aclose = AsyncMock()
    reporter = mock.Mock()
    reporter.run_forever = lambda: asyncio.Event().wait()
    reporter.notifier.aclose = AsyncMock()

    await cli_runners._serve(runner, service, reporter)

    assert asyncio.all_tasks() == {asyncio.current_task()}  # relatório cancelado
    reporter.notifier.aclose.assert_awaited_once()
    service.aclose.assert_awaited_once()


async def test_the_trade_runner_runs_its_hub_and_closes_its_candles():
    candles = NoCandles()
    candles.aclose = AsyncMock()
    runner = _runner(candles=candles)
    runner.hub.run = AsyncMock()
    task = asyncio.create_task(runner.serve())
    while not connection_path("paper").exists():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runner.hub.run.assert_awaited_once()
    candles.aclose.assert_awaited_once()  # o cliente da Jupiter também
    assert not connection_path("paper").exists()


async def test_strategy_runners_share_the_trade_runners_price_hub():
    runner = _runner(price="150")
    api = runner.hub.client
    assert isinstance(api, PriceTable)  # a Price API falsa de `trade_runner`
    async with await runner.start() as server:
        client = await _client(runner, server)
        assert await client.price(SOL.mint) == Decimal(150)
        assert await client.price(SOL.mint) == Decimal(150)
        assert api.calls == 1  # o segundo pedido veio do hub, não da API
        with pytest.raises(TradeServiceError, match="StalePriceError"):
            await client.price(USDC.mint)  # sem preço: nenhuma decisão
        await client.aclose()


async def test_concurrent_calls_on_one_connection_do_not_cross():
    # um par sem stablecoin pede o preço do token e o da cotação juntos
    runner = _runner(price="150")
    async with await runner.start() as server:
        client = await _client(runner, server)
        calls = asyncio.gather(*(client.price(SOL.mint) for _ in range(3)))
        prices = await asyncio.wait_for(calls, 5)  # sem a trava, travava aqui
        assert prices == [Decimal(150)] * 3
        await client.aclose()


def test_connect_takes_prices_from_the_trade_runner(monkeypatch, tmp_path):
    data_dir().mkdir(parents=True, exist_ok=True)
    info = {"host": "127.0.0.1", "port": 1, "token": "t", "pid": 1}
    (connection_path("paper")).write_text(json.dumps(info), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")

    with mock.patch("trader.strategy.runner.AsyncWebsocketTradingBot") as bot:
        CliRunner().invoke(main_module.app, ["connect", str(spec)])

    config = bot.call_args.args[0]
    assert config.market.price_of == config.trader.price


class _Candles:
    """A Jupiter de mentira do trade-runner: `qty` candles, um por minuto."""

    def __init__(self):
        self.calls = []

    async def get_candles(self, mint, interval, candle_qty):
        self.calls.append((mint, interval, candle_qty))
        return [
            TickerData(
                timestamp=T0 + timedelta(minutes=i),
                open=Decimal("150.1234567890"),
                high=Decimal("151.5"),
                low=Decimal("149.25"),
                last=Decimal(150 + i),
            )
            for i in range(candle_qty)
        ]

    async def aclose(self):
        return None


async def test_candles_come_from_the_trade_runner():
    # B12: o strategy-runner não fala com a Jupiter; o aquecimento vem do op
    jupiter = _Candles()
    runner = _runner(candles=jupiter)
    async with await runner.start() as server:
        client = await _client(runner, server)
        got = await RemoteCandles(client).get_candles(SOL.mint, Interval.MINUTE_1, 3)
        await client.aclose()
    assert jupiter.calls == [(SOL.mint, Interval.MINUTE_1, 3)]
    assert got == await _Candles().get_candles(SOL.mint, Interval.MINUTE_1, 3)


async def test_a_full_warm_up_fits_in_one_reply():
    # 900 barras passam do limite de linha padrão do asyncio (64 KB)
    runner = _runner(candles=_Candles())
    async with await runner.start() as server:
        client = await _client(runner, server)
        got = await client.candles(SOL.mint, Interval.SECOND_15, 900)
        await client.aclose()
    assert len(got) == 900
    assert len(wire.encode({"candles": wire.candles_to_list(got)})) > 64 * 1024


@pytest.mark.parametrize("qty", [0, 1001])
async def test_a_candle_count_out_of_range_is_refused(qty):
    runner = _runner(candles=_Candles())
    async with await runner.start() as server:
        client = await _client(runner, server)
        with pytest.raises(TradeServiceError, match="qty"):
            await client.candles(SOL.mint, Interval.MINUTE_1, qty)
        await client.aclose()


def test_connect_takes_its_candles_from_the_trade_runner():
    path = connection_path("paper")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 1, "token": "t"}))
    strategy = SpecStrategy(StrategySpec.model_validate(SPEC))
    bot = strategy_bot(strategy, path, mock.Mock())
    assert isinstance(bot.market, HubMarketData)
    assert isinstance(bot.market.candles, RemoteCandles)
    assert bot.market.candles.trader is bot.trader


async def test_serve_resolves_dead_intents_before_it_accepts_connections():
    runner = _runner()
    order = []
    runner.service.resolve_intents = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: order.append("resolve")
    )

    async def start():
        order.append("start")
        raise RuntimeError("parou")

    runner.start = start  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="parou"):
        await runner.serve()
    assert order == ["resolve", "start"]


async def test_the_sweep_resolves_an_intent_that_was_never_sent():
    from trader.execution.models.intent import PolicyDecision

    runner = _runner()
    ledger = runner.service.gateway.ledger
    intent = make_intent(account="paper:strategy:x")
    ledger.record_intent(intent, PolicyDecision(True))
    ledger.mark_unconfirmed(intent.intent_id, "interrompida: CancelledError")

    await runner.sweep()

    assert ledger.get(intent.intent_id).status == IntentStatus.FAILED  # type: ignore[union-attr]
