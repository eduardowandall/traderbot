import logging
import os
import subprocess
import sys

from trader.shared import logging_config
from trader.shared.paths import PROJECT_ROOT


def test_http_loggers_do_not_log_urls_with_secrets():
    loggers = logging_config.LOGGING["loggers"]
    # httpx (INFO) loga a HELIUS_RPC_URL com api-key e a URL do Telegram
    # com o token do bot
    # o solana-py usa o fork httpx2, que também loga a URL completa
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        level = logging.getLevelNamesMapping()[loggers[name]["level"]]
        assert level >= logging.WARNING, name


def test_secrets_are_redacted_even_in_tracebacks():
    url = "https://mainnet.helius-rpc.com/?api-key=abc-123"
    telegram = "https://api.telegram.org/bot123456:AA-secret_x/sendMessage"
    formatter = logging_config.RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"falhou {url}")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "t",
            logging.ERROR,
            __file__,
            1,
            "POST %s %s",
            (url, telegram),
            sys.exc_info(),
        )

    text = formatter.format(record)

    assert "abc-123" not in text and "AA-secret_x" not in text
    assert "api-key=***" in text and "/bot***/sendMessage" in text


def test_console_logs_go_to_stderr_not_stdout():
    # o stdout é reservado para as saídas `--json` dos comandos de agente
    handler = logging_config.stderr_rich_handler()
    assert handler.console.stderr
    assert logging_config.LOGGING["handlers"]["console"]["()"] is (
        logging_config.stderr_rich_handler
    )


def test_console_filter_allows_bot_logs_and_warnings_only():
    f = logging_config.ConsoleFilter()

    def record(name, level):
        return logging.LogRecord(name, level, __file__, 1, "msg", None, None)

    assert f.filter(record("bot", logging.DEBUG))
    assert f.filter(record("trader.strategy.spec.strategy", logging.DEBUG))
    assert not f.filter(record("trader.execution.gateway.account", logging.INFO))
    assert f.filter(record("trader.execution.gateway.account", logging.WARNING))


def test_file_lines_carry_the_bot_name(tmp_path):
    # o `isolated_workdir` faz chdir para tmp_path: `.logs/` fica lá
    handler = logging_config.file_handler()
    handler.addFilter(logging_config.BotNameFilter())
    handler.setFormatter(
        logging.Formatter(logging_config.LOGGING["formatters"]["default"]["format"])
    )
    logger = logging.getLogger("test.logging_config")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("fora de um bot")
        token = logging_config.botname.set("paper-run-random-SOL-USDC")
        logger.info("dentro do bot")
        logging_config.botname.reset(token)
    finally:
        logger.removeHandler(handler)
        handler.close()

    (log_file,) = (tmp_path / ".logs").glob("trader-*.log")
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert "[-]" in lines[0]
    assert "[paper-run-random-SOL-USDC]" in lines[1]


def test_uncaught_cli_errors_are_redacted(capsys):
    from trader.api.cli import app

    try:
        raise RuntimeError("falhou em https://mainnet.helius-rpc.com/?api-key=SEGREDO")
    except RuntimeError as ex:
        logging_config.redacted_excepthook(type(ex), ex, ex.__traceback__)

    err = capsys.readouterr().err
    assert "SEGREDO" not in err and "api-key=***" in err
    # o traceback "bonito" do Typer não passaria pela redação
    assert app.pretty_exceptions_enable is False


def test_old_log_files_are_pruned(tmp_path):
    import os
    import time

    old = tmp_path / "trader-1-1.log"
    old_rotated = tmp_path / "trader-1-1.log.3"
    fresh = tmp_path / "trader-2-2.log"
    other = tmp_path / "paper-run-x.log"  # não é nosso padrão: fica
    for path in (old, old_rotated, fresh, other):
        path.write_text("x", encoding="utf-8")
    month_ago = time.time() - 30 * 86400
    for path in (old, old_rotated, other):
        os.utime(path, (month_ago, month_ago))

    assert logging_config.prune_logs(tmp_path) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "paper-run-x.log",
        "trader-2-2.log",
    ]


def test_an_error_escaping_the_real_cli_process_is_redacted(tmp_path):
    # T3: o Typer reinstala o excepthook dele; só um processo de verdade mostra
    # o que chega ao terminal
    script = tmp_path / "boom.py"
    script.write_text(
        "import sys\n"
        "import main\n"
        "from trader.api.cli import app\n"
        "@app.command('boom')\n"
        "def boom():\n"
        "    raise RuntimeError('https://mainnet.helius-rpc.com/?api-key=SEGREDO')\n"
        "sys.argv = ['main.py', 'boom']\n"
        "main.main()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        env=os.environ | {"PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert proc.returncode == 1
    assert "RuntimeError" in proc.stderr
    assert "SEGREDO" not in proc.stderr + proc.stdout
    assert "api-key=***" in proc.stderr
