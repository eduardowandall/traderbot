import asyncio
import os
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer

from trader import logging_config
from trader.agent_api.cli import market_app, strategy_app
from trader.backtest import Backtester, TickRecorder, load_ticks, ticks_from_candles
from trader.bot.async_websocket_bot import AsyncWebsocketTradingBot
from trader.bot.config import BotConfig
from trader.execution import KillSwitch, TradeGateway
from trader.ledger import Ledger, ledger_path
from trader.market import JupiterMarketData
from trader.models import SOLANA_MINTS, Interval
from trader.models.intent import (
    IntentSide,
    IntentStatus,
    TradeIntent,
    with_idempotency_key,
)
from trader.models.mode import RunningMode
from trader.notification.notification_service import (
    NullNotificationService,
    TelegramNotificationService,
)
from trader.paper import (
    DEFAULT_PAPER_BALANCES,
    SimulatedWallet,
    parse_balances,
)
from trader.policy import Policy
from trader.providers.jupiter.async_jupiter_svc import (
    DEFAULT_MAX_PRICE_IMPACT_PCT,
    AsyncJupiterProvider,
)
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.strategies_registry import get_strategy_factory
from trader.trading_service.local import LocalTradeClient
from trader.wiring import (
    build_gateway,
    build_provider,
    build_trade_service,
    paper_wallet_path,
)

app = typer.Typer()
ledger_app = typer.Typer(help="Consulta e manutenção do ledger de trades.")
app.add_typer(ledger_app, name="ledger")
paper_app = typer.Typer(help="Carteira simulada do modo paper.")
app.add_typer(paper_app, name="paper")
# comandos de agente: sempre respondem em JSON (trader/agent_api/cli.py)
app.add_typer(market_app, name="market")
app.add_typer(strategy_app, name="strategy")


def _warn(message: str) -> None:
    # stderr: não pode sujar o stdout dos comandos `--json`
    typer.echo(message, err=True)


def _build_provider(mode: RunningMode, **limits) -> AsyncJupiterProvider:
    return build_provider(mode, on_wallet_created=_warn, **limits)


@app.command()
def run(
    mode: RunningMode = typer.Argument(
        RunningMode.DRY, help="Modo de execucão do bot."
    ),
    currency: str = typer.Argument(
        "SOL-USDC", help="The trading symbol tuple (ex: SOL-USDC)"
    ),
    strategy: str = typer.Argument(..., help="The trading strategy to use"),
    notification_service: str = typer.Option(
        "null", help="Serviço de notificação: 'telegram' ou 'null'"
    ),
    notification_args: str | None = typer.Option(
        None, help="Argumentos do serviço de notificação"
    ),
    strategy_args: str | None = typer.Argument(None, help="Argumentos da estratégia"),
    record_ticks: Path | None = typer.Option(
        None, help="Grava cada preço recebido neste CSV (para `backtest --ticks`)"
    ),
):
    """
    Executa o bot em modo produção.

    Exemplos:
        uv run main.py run dry SOL-USDC random 'sell_chance=20 buy_chance=40'
        uv run main.py run dry SOL-USDC composer 'buy_mode=all sell_mode=any'
    """

    # estratégia primeiro: um erro nela não pode deixar ledger/conexões abertos
    strategy_obj = _get_strategy_obj(strategy, strategy_args)
    _check_symbol(strategy_obj, currency)
    token, quote = SOLANA_MINTS.get_pair(currency)
    notification_svc = _get_notification_svc(notification_service, notification_args)

    # execução (modo, chave, ledger) de um lado; o bot só vê o bucket do par
    service = build_trade_service(mode, on_wallet_created=_warn)
    trader = LocalTradeClient(
        service,
        # o nome do bucket é o par: a conta no ledger segue "<modo>:<par>"
        currency,
        quote.mint,
        token.mint,
        source=f"run-{strategy}",
        owns_service=True,
    )
    recorder = TickRecorder(record_ticks) if record_ticks else None
    config = BotConfig(
        name=f"{mode}-run-{strategy}",
        symbol=currency,
        strategy=strategy_obj,
        market=JupiterMarketData(),
        trader=trader,
        notifier=notification_svc,
        on_tick=recorder.record if recorder else None,
    )
    assert service.gateway is not None
    with service.gateway, recorder or nullcontext():
        _run_bot(AsyncWebsocketTradingBot(config))


def _check_symbol(strategy_obj, symbol: str) -> None:
    # estratégias com par próprio (specs) só rodam no par delas
    expected = getattr(strategy_obj, "symbol", None)
    if expected and expected != symbol:
        raise typer.BadParameter(f"a estratégia é para {expected}, não {symbol}")


def _run_bot(bot: AsyncWebsocketTradingBot) -> None:
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()


@app.command()
def swap(
    mode: RunningMode = typer.Argument(
        RunningMode.DRY, help="Modo de execucão do bot."
    ),
    symbol_in: str = typer.Argument(..., help="Symbol to spend (ex: SOL)"),
    symbol_out: str = typer.Argument(..., help="Symbol to receive (ex: USDC)"),
    quantity: str = typer.Argument(..., help="Amount of symbol_in to swap"),
    slippage_bps: int = typer.Option(
        50, min=0, max=1000, help="Slippage tolerance in basis points"
    ),
    max_price_impact: str = typer.Option(
        str(DEFAULT_MAX_PRICE_IMPACT_PCT),
        help="Impacto de preço máximo aceito, em % (ex: 1 = 1%)",
    ),
    idempotency_key: str | None = typer.Option(
        None, help="Chave para não repetir o mesmo swap (padrão: aleatória)"
    ),
):
    """
    Executa um swap manual entre dois símbolos.

    Exemplos:
        uv run main.py swap dry JUP USDC 1000
        uv run main.py swap real SOL USDC 0.5 --slippage-bps 100
    """

    amount = _parse_decimal(quantity, "quantity")
    if amount <= 0:
        raise typer.BadParameter("quantity deve ser maior que zero")
    max_impact = _parse_decimal(max_price_impact, "max-price-impact")

    provider = _build_provider(mode, max_price_impact_pct=max_impact)
    gateway = build_gateway(mode)
    with gateway:
        signature = asyncio.run(
            _execute_swap(
                provider,
                gateway,
                f"{mode}:swap",
                symbol_in,
                symbol_out,
                amount,
                slippage_bps,
                idempotency_key,
            )
        )
    typer.echo(f"Swap executado: {signature}")


async def _execute_swap(
    provider: AsyncJupiterProvider,
    gateway: TradeGateway,
    account: str,
    symbol_in: str,
    symbol_out: str,
    quantity: Decimal,
    slippage_bps: int,
    idempotency_key: str | None = None,
) -> str:
    mint_in = SOLANA_MINTS.get_by_symbol(symbol_in)
    mint_out = SOLANA_MINTS.get_by_symbol(symbol_out)
    raw_quantity = mint_in.ui_to_raw(quantity)
    intent = TradeIntent(
        source="cli",
        account=account,
        side=IntentSide.SWAP,
        spend_mint=mint_in.mint,
        receive_mint=mint_out.mint,
        spend_amount=quantity,
        # só dá para estimar em USD quando o token gasto é stablecoin
        notional_usd=quantity if mint_in.is_usd_stable else None,
    )
    intent = with_idempotency_key(intent, idempotency_key)
    try:
        result = await gateway.submit(
            intent,
            lambda: provider.swap_with_details(
                mint_in.mint, mint_out.mint, raw_quantity, slippage_bps
            ),
        )
        return result.signature
    finally:
        await provider.aclose()


@app.command()
def halt(reason: str = typer.Argument("manual", help="Motivo da parada")):
    """Ativa o kill switch: nenhum trade executa até `resume`."""
    KillSwitch().activate(reason)
    for mode in RunningMode:
        # `Policy()` em vez do policy.toml: o kill switch precisa funcionar
        # mesmo com a política quebrada
        with TradeGateway.for_mode(mode, policy=Policy()) as gateway:
            gateway.add_event("halt", {"reason": reason})
    typer.echo(f"Kill switch ATIVO ({reason}).")


@app.command()
def resume(
    mode: RunningMode = typer.Argument(RunningMode.DRY, help="Ledger a rearmar"),
    note: str = typer.Option("", help="Observação para o ledger"),
):
    """Desativa o kill switch e rearma o circuit breaker do modo."""
    gateway = build_gateway(mode)
    with gateway:
        gateway.resume(note)
    typer.echo(f"Kill switch desativado; circuit breaker de {mode} rearmado.")


@paper_app.command("balance")
def paper_balance():
    """Mostra os saldos da carteira simulada."""
    wallet = SimulatedWallet(paper_wallet_path())
    if wallet.is_empty:
        typer.echo("Carteira paper vazia (use `main.py paper reset`).")
        return
    for mint, amount in wallet.balances().items():
        typer.echo(f"{SOLANA_MINTS.symbol_of(mint):>8} {amount}")


@paper_app.command("reset")
def paper_reset(
    balances: str = typer.Argument(
        " ".join(f"{s}={v}" for s, v in DEFAULT_PAPER_BALANCES.items()),
        help="Saldos iniciais, ex: 'USDC=100 SOL=0.5'",
    ),
):
    """Recria a carteira simulada com os saldos informados."""
    try:
        parsed = parse_balances(balances)
    except ValueError as ex:
        raise typer.BadParameter(str(ex)) from ex
    SimulatedWallet(paper_wallet_path()).reset(parsed)
    with TradeGateway.for_mode(RunningMode.PAPER, policy=Policy()) as gateway:
        gateway.add_event("paper_wallet_reset", {"balances": parsed})
    typer.echo(f"Carteira paper: {parsed}")
    typer.echo(
        "Atenção: posições abertas no ledger paper não são apagadas; "
        "o bot vai sinalizar a diferença ao iniciar."
    )


@app.command()
def backtest(
    symbol: str = typer.Argument(..., help="Par OUTPUT-INPUT, ex: SOL-USDC"),
    strategy: str = typer.Argument(..., help="Estratégia (random, composer, ...)"),
    strategy_args: str | None = typer.Argument(None, help="Argumentos da estratégia"),
    ticks: Path | None = typer.Option(
        None, help="CSV de ticks gravado com `run --record-ticks`"
    ),
    candles: int = typer.Option(
        0, min=0, help="Sem --ticks: baixa N candles da Jupiter e usa o fechamento"
    ),
    interval: Interval = typer.Option(Interval.MINUTE_1, help="Intervalo dos candles"),
    balance: str = typer.Option("100", help="Saldo inicial na stablecoin de entrada"),
    fee_bps: str = typer.Option("30", help="Custo por swap em bps (taxa + slippage)"),
    seed: str = typer.Option("0", help="Semente das estratégias aleatórias"),
):
    """
    Reproduz ticks por uma estratégia, com carteira simulada.

    Exemplos:
        uv run main.py backtest SOL-USDC random 'sell_chance=5 buy_chance=5' --ticks ticks.csv
        uv run main.py backtest SOL-USDC composer --candles 1000 --interval 1_MINUTE
    """
    if ticks is None and not candles:
        raise typer.BadParameter("informe --ticks ARQUIVO ou --candles N")
    strategy_obj = _get_strategy_obj(strategy, strategy_args)
    if ticks is not None:
        tick_list = load_ticks(ticks)
    else:
        token, _ = SOLANA_MINTS.get_pair(symbol)
        tick_list = ticks_from_candles(
            asyncio.run(_fetch_candles(token.mint, interval, candles))
        )

    result = asyncio.run(
        Backtester(
            strategy_obj,
            symbol,
            tick_list,
            initial_balance=_parse_decimal(balance, "balance"),
            fee_bps=_parse_decimal(fee_bps, "fee-bps"),
            seed=seed,
        ).run()
    )
    typer.echo(result.summary())


async def _fetch_candles(mint: str, interval: Interval, qty: int):
    # só leitura de dados públicos: sem chave, sem RPC
    data = JupiterMarketData()
    try:
        return await data.get_candles(mint, interval, qty)
    finally:
        await data.aclose()


@ledger_app.command("list")
def ledger_list(
    mode: RunningMode = typer.Argument(RunningMode.DRY),
    limit: int = typer.Option(20, min=1),
):
    """Lista as intenções mais recentes."""
    with Ledger(ledger_path(mode)) as ledger:
        for record in ledger.list_intents(limit):
            intent = record.intent
            typer.echo(
                f"{intent.created_at:%Y-%m-%d %H:%M:%S} {intent.intent_id[:8]} "
                f"{record.status:<11} {intent.side:<4} {intent.spend_amount} "
                f"{SOLANA_MINTS.symbol_of(intent.spend_mint)} -> "
                f"{SOLANA_MINTS.symbol_of(intent.receive_mint)}"
                f" [{intent.source}]{_record_costs(record)}{_record_pnl(record)}"
            )
            for reason in record.decision_reasons:
                typer.echo(f"    recusa: {reason}")
            if record.error:
                typer.echo(f"    erro: {record.error}")


@ledger_app.command("verify")
def ledger_verify(mode: RunningMode = typer.Argument(RunningMode.DRY)):
    """Confere a cadeia de hashes dos eventos."""
    with Ledger(ledger_path(mode)) as ledger:
        bad = ledger.verify_chain()
    if bad is not None:
        typer.echo(f"Ledger ADULTERADO a partir do evento {bad}.")
        raise typer.Exit(1)
    typer.echo("Ledger íntegro.")


@ledger_app.command("resolve")
def ledger_resolve(
    mode: RunningMode = typer.Argument(...),
    intent_id: str = typer.Argument(..., help="Id (completo) da intenção"),
    status: str = typer.Argument(..., help="executed ou failed"),
    note: str = typer.Option(..., help="Como foi verificado (ex: explorer)"),
):
    """Resolve manualmente uma intenção sem confirmação."""
    try:
        parsed = IntentStatus(status)
    except ValueError as ex:
        raise typer.BadParameter("status deve ser executed ou failed") from ex
    with Ledger(ledger_path(mode)) as ledger:
        ledger.resolve(intent_id, parsed, note)
        if parsed == IntentStatus.FAILED and mode == RunningMode.REAL:
            # transações que falham também pagam taxa
            asyncio.run(_backfill_failed_fee(ledger, intent_id))
    typer.echo(f"Intenção {intent_id} resolvida como {parsed}.")


async def _backfill_failed_fee(ledger: Ledger, intent_id: str) -> None:
    record = ledger.get(intent_id)
    if record is None or not record.signature:
        return
    rpc = AsyncRPCClient()
    try:
        tx = await rpc.get_confirmed_transaction(record.signature)
    finally:
        await rpc.aclose()
    meta = getattr(tx, "meta", None) if tx else None
    if meta is None:
        typer.echo("Transação não encontrada: nenhuma taxa foi paga.")
        return
    ledger.record_failed_fee(intent_id, meta.fee)
    typer.echo(f"Taxa da transação que falhou registrada: {meta.fee} lamports.")


def _record_costs(record) -> str:
    if record.fee_lamports is None:
        return ""
    paid = (record.fee_lamports or 0) + (record.rent_lamports or 0)
    paid += record.other_lamports or 0
    return f" custos={Decimal(paid) / Decimal(10**9):.9f}SOL[{record.costs_source}]"


def _record_pnl(record) -> str:
    if record.realized_pnl_usd is None:
        return ""
    text = f" pnl~${record.realized_pnl_usd:+.4f}"
    if record.net_pnl_quote is not None and record.quote_mint:
        text += (
            f" líquido={record.net_pnl_quote:+.6f}"
            f"{SOLANA_MINTS.symbol_of(record.quote_mint)}"
        )
    return text


@app.command()
def pnl(
    mode: RunningMode = typer.Argument(RunningMode.PAPER),
    account: str | None = typer.Option(None, help="Só esta conta (ex: paper:SOL-USDC)"),
):
    """PnL líquido por conta: nativo (token de cotação), custos em SOL e ~USD."""
    with Ledger(ledger_path(mode)) as ledger:
        report = ledger.pnl_report(account)
    if not report:
        typer.echo("Nenhum trade executado.")
        return
    for entry in report.values():
        typer.echo(_format_account_pnl(entry))
    typer.echo(_format_totals(report.values()))


def _format_account_pnl(entry) -> str:
    sym = entry.quote_symbol
    lines = [
        f"{entry.account}: {entry.trades} pernas, {entry.closed} posições fechadas",
        f"  bruto   {entry.gross_quote:+.6f} {sym}",
        f"  custos  {entry.costs_sol:.9f} SOL (das posições fechadas)",
        f"  líquido {entry.net_quote:+.6f} {sym}   (~${entry.net_usd:+.4f})",
        f"  pago em SOL: taxas {Decimal(entry.fee_lamports) / 10**9:.9f} "
        f"(priority {Decimal(entry.priority_fee_lamports) / 10**9:.9f}), "
        f"rent {Decimal(entry.rent_lamports) / 10**9:.9f} (reembolsável), "
        f"outros {Decimal(entry.other_lamports) / 10**9:.9f}, "
        f"txs com falha {Decimal(entry.failed_fee_lamports) / 10**9:.9f}",
    ]
    if entry.incomplete or entry.unknown_costs:
        lines.append(
            f"  [!] {entry.incomplete} posição(ões) sem custos convertidos, "
            f"{entry.unknown_costs} perna(s) sem custos reais"
        )
    return "\n".join(lines)


def _format_totals(entries) -> str:
    by_quote: dict[str, Decimal] = {}
    usd = Decimal("0")
    paid = Decimal("0")
    for entry in entries:
        by_quote[entry.quote_symbol] = (
            by_quote.get(entry.quote_symbol, Decimal("0")) + entry.net_quote
        )
        usd += entry.net_usd
        paid += entry.paid_sol
    native = ", ".join(f"{net:+.6f} {sym}" for sym, net in by_quote.items())
    return f"TOTAL líquido: {native} (~${usd:+.4f}); pago em SOL: {paid:.9f}"


def _parse_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as ex:
        raise typer.BadParameter(f"{name} inválido: {value!r}") from ex
    if not parsed.is_finite():
        raise typer.BadParameter(f"{name} inválido: {value!r}")
    return parsed


def _get_strategy_obj(strategy: str, strategy_args: str | None = None):
    strategy_cls = get_strategy_factory(strategy)
    try:
        args = __parse_kwargs(strategy_args.split()) if strategy_args else {}
        return strategy_cls(**args)
    except ValueError as ex:
        raise Exception(f"Erro ao configurar estratégia: {ex}") from ex


def _get_notification_svc(
    notification_service: str, notification_args: str | None = None
):
    if notification_service == "telegram":
        args = __parse_kwargs(notification_args.split()) if notification_args else {}
        # preferir variáveis de ambiente: argumentos de CLI ficam visíveis na
        # lista de processos e no histórico do shell
        chat_id = args.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")
        token = args.get("token") or os.getenv("TELEGRAM_BOT_TOKEN")
        if not chat_id or not token:
            raise ValueError(
                "Para notificação via Telegram, é necessário informar chat_id e token "
                "(TELEGRAM_CHAT_ID / TELEGRAM_BOT_TOKEN)"
            )
        return TelegramNotificationService(str(chat_id), str(token))
    return NullNotificationService()


def __parse_kwargs(argv: list[str]) -> dict[str, str]:
    kwargs = {}

    for arg in argv:
        key_value = arg.split("=", 1)
        if len(key_value) == 2:
            key, value = key_value
            kwargs[key] = value
        else:
            kwargs[key_value[0]] = True  # flag booleana

    return kwargs


if __name__ == "__main__":
    logging_config.setup_logging()
    app()
