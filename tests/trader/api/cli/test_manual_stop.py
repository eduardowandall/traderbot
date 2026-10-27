"""A33: a parada manual (`main.py stop`), pelo op `stop` do trade-runner."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock

import pytest
from factories import make_spec, open_ledger, terms_of, trade_runner
from typer.testing import CliRunner

import main as main_module
from trader.api.cli.runners import stop_line
from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.ledger import events
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.paths import connection_path, data_dir
from trader.shared.trading_service.protocol import (
    BucketStatus,
    OrderRequest,
    ReplyStatus,
    TradeServiceError,
)
from trader.strategy.bot.decision import bucket_done
from trader.strategy.trading_service.remote import RemoteTradeClient, stop_specs

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
)
SPEC = make_spec(ttl_days=5, expires_at=None)
BUY = OrderRequest(OrderSide.BUY, Decimal("0.2"), Decimal("100"))


def _service(ledger) -> TradeService:
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(T0, Decimal(100))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    gateway = TradeGateway(ledger, LOOSE, False)
    return TradeService(
        SpotVenue(paper_provider(wallet, jupiter_client=quotes)), gateway, mode="paper"
    )


def _runner(ledger=None):
    return trade_runner(_service(ledger or open_ledger()), None, {SOL.mint: 100})


def _address(runner, server, token=None) -> dict:
    port = server.sockets[0].getsockname()[1]
    return {"host": "127.0.0.1", "port": port, "token": token or runner.token}


async def _client(runner, server, spec=SPEC) -> RemoteTradeClient:
    address = _address(runner, server)
    client = RemoteTradeClient(
        address["host"], address["port"], address["token"], terms_of(spec)
    )
    await client.open()
    return client


def _sells(runner) -> int:
    intents = runner.service.gateway.ledger.list_intents()
    return sum(1 for r in intents if r.intent.side == "sell")


async def test_stop_sells_the_open_position_once_and_the_bot_exits():
    runner = _runner()
    async with await runner.start() as server:
        client = await _client(runner, server)
        assert (await client.submit(BUY)).filled

        stopped = await stop_specs(_address(runner, server), terms_of(SPEC))
        again = await stop_specs(_address(runner, server), terms_of(SPEC))
        snapshot = await client.bucket()  # o `connect` vivo vê o bucket parado
        refused = await client.submit(BUY)
        await client.aclose()

    assert [i["outcome"] for i in stopped] == ["sold"]
    assert [i["outcome"] for i in again] == ["flat"]
    assert _sells(runner) == 1
    assert snapshot.status == BucketStatus.RETIRING and bucket_done(snapshot)
    assert refused.status == ReplyStatus.REJECTED
    ledger = runner.service.gateway.ledger
    account = runner.service.account_id(stopped[0]["bucket"])
    assert ledger.last_event(events.BUCKET_STOPPED, account) == {"account": account}


async def test_stop_opens_a_spec_that_never_connected():
    runner = _runner()
    async with await runner.start() as server:
        stopped = await stop_specs(_address(runner, server), terms_of(SPEC))
    name = f"strategy:{terms_of(SPEC).spec_id}"
    assert stopped == [{"bucket": name, "spec": SPEC["name"], "outcome": "flat"}]
    assert (await runner.service.get_bucket(name)).status == BucketStatus.RETIRING


async def test_stop_all_stops_every_open_bucket():
    runner = _runner()
    other = {**SPEC, "budget_usd": 40}
    async with await runner.start() as server:
        first = await _client(runner, server)
        second = await _client(runner, server, spec=other)
        assert (await first.submit(BUY)).filled
        stopped = await stop_specs(_address(runner, server), None)
        await first.aclose()
        await second.aclose()
    assert sorted(i["outcome"] for i in stopped) == ["flat", "sold"]


async def test_stop_needs_the_token():
    runner = _runner()
    async with await runner.start() as server:
        with pytest.raises(TradeServiceError, match="token"):
            await stop_specs(_address(runner, server, token="nope"), None)
    assert not runner.specs


async def test_a_stopped_bucket_stays_stopped_after_a_restart():
    ledger = open_ledger()
    name = f"strategy:{terms_of(SPEC).spec_id}"
    first = _service(ledger)
    await first.open_bucket(name, str(USDC.mint), str(SOL.mint), Decimal(50))
    first.stop(name)
    first.stop(name)  # uma vez no ledger

    again = _service(ledger)  # um `serve` reiniciado, o `connect` de volta
    await again.open_bucket(name, str(USDC.mint), str(SOL.mint), Decimal(50))
    reply = await again.submit_order(name, BUY)

    assert (await again.get_bucket(name)).status == BucketStatus.RETIRING
    assert reply.status == ReplyStatus.REJECTED
    stops = ledger.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?", (events.BUCKET_STOPPED,)
    ).fetchone()[0]
    assert stops == 1
    ledger.close()


# --- o CLI -----------------------------------------------------------------------


def _connection_file() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    info = {"host": "127.0.0.1", "port": 1, "token": "t", "pid": 1}
    connection_path("paper").write_text(json.dumps(info), encoding="utf-8")


def test_the_cli_stops_one_spec(tmp_path):
    _connection_file()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    item = {"bucket": "strategy:x", "spec": "s", "outcome": "sold"}
    target = "trader.api.cli.runners.stop_specs"
    with mock.patch(target, mock.AsyncMock(return_value=[item])) as stop:
        result = CliRunner().invoke(main_module.app, ["stop", str(spec)])
    assert result.exit_code == 0, result.output
    assert "posição vendida" in result.output
    connection, terms = stop.call_args.args
    assert connection["token"] == "t" and terms == terms_of(SPEC)


@pytest.mark.parametrize("outcome", ["denied", "no_price"])
def test_the_cli_fails_when_a_sale_did_not_happen(outcome):
    _connection_file()
    item = {"bucket": "strategy:x", "spec": "s", "outcome": outcome}
    target = "trader.api.cli.runners.stop_specs"
    with mock.patch(target, mock.AsyncMock(return_value=[item])) as stop:
        result = CliRunner().invoke(main_module.app, ["stop", "--all"])
    assert result.exit_code == 1
    assert stop.call_args.args[1] is None  # --all


@pytest.mark.parametrize("args", [[], ["spec.json", "--all"]])
def test_the_cli_takes_a_spec_or_all(args):
    result = CliRunner().invoke(main_module.app, ["stop", *args])
    assert result.exit_code != 0
    assert "exatamente um" in result.output


def test_a_refused_sale_reads_with_its_reasons():
    item = {"bucket": "b", "spec": "s", "outcome": "denied", "reasons": ["limite"]}
    assert stop_line(item) == (
        "Parada s (b): venda denied (limite): a varredura tenta de novo"
    )


async def test_an_old_serve_without_the_stop_op_says_so():
    # um `serve` de antes da A33 responde "mande `hello` primeiro"
    async def old_serve(reader, writer):
        await reader.readline()
        error = "HelloError: mande `hello` primeiro"
        writer.write(
            json.dumps({"ok": False, "error": error, "kind": "HelloError"}).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(old_serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        address = {"host": "127.0.0.1", "port": port, "token": "t"}
        with pytest.raises(TradeServiceError, match="reinicie o serve"):
            await stop_specs(address, None)
