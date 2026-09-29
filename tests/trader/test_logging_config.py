import logging

from trader import logging_config


def test_http_loggers_do_not_log_urls_with_secrets():
    loggers = logging_config.LOGGING["loggers"]
    # httpx (INFO) loga a HELIUS_RPC_URL com api-key; urllib3 (DEBUG) loga o
    # path da URL do Telegram com o token do bot
    for name in ("httpx", "urllib3"):
        level = logging.getLevelNamesMapping()[loggers[name]["level"]]
        assert level >= logging.WARNING, name


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
    assert f.filter(record("trader.trading_strategy", logging.DEBUG))
    assert not f.filter(record("trader.async_account", logging.INFO))
    assert f.filter(record("trader.async_account", logging.WARNING))
