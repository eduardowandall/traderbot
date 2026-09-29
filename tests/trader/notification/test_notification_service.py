import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx

from trader.notification.notification_service import (
    NotificationService,
    NullNotificationService,
    TelegramNotificationService,
)

URL = "https://api.telegram.org/bottoken123/sendMessage"


def _ok():
    return httpx.Response(200, request=httpx.Request("POST", URL))


def test_null_notification_service_is_noop():
    service = NullNotificationService()
    assert isinstance(service, NotificationService)
    assert service.send_message("ignored") is None
    asyncio.run(service.aclose())


def test_telegram_init():
    service = TelegramNotificationService("12345", "token123")
    assert service.chat_id == "12345"
    assert service.token == "token123"
    assert service.url == "https://api.telegram.org/bottoken123"


def test_outside_a_loop_sends_right_away():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.return_value = _ok()
        TelegramNotificationService("12345", "token123").send_message("Hello World")

    post.assert_awaited_once_with(URL, data={"chat_id": "12345", "text": "Hello World"})


async def test_inside_a_loop_it_never_blocks_and_aclose_drains():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_post(*args, **kwargs):
        started.set()
        await release.wait()
        return _ok()

    service = TelegramNotificationService("12345", "token123")
    with patch("httpx.AsyncClient.post", side_effect=slow_post) as post:
        service.send_message("Hello World")  # volta na hora: é uma task
        await started.wait()
        assert service._pending
        release.set()
        await service.aclose()

    post.assert_called_once()
    assert not service._pending


async def test_aclose_gives_up_on_a_stuck_send(monkeypatch):
    monkeypatch.setattr(
        "trader.notification.notification_service.CLOSE_TIMEOUT_SECONDS", 0.01
    )

    async def stuck(*args, **kwargs):
        await asyncio.sleep(10)

    service = TelegramNotificationService("12345", "token123")
    with patch("httpx.AsyncClient.post", side_effect=stuck):
        service.send_message("Hello World")
        await service.aclose()  # não trava o encerramento do bot
        assert service._pending  # desistiu de esperar
        pending = list(service._pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def test_errors_are_logged_not_raised(caplog):
    with (
        patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("down")),
        caplog.at_level(logging.INFO),
    ):
        TelegramNotificationService("12345", "token123").send_message("Hello World")

    assert "Erro ao enviar alerta Telegram:" in caplog.text


def test_error_log_does_not_leak_token(caplog):
    error = httpx.ConnectError(
        "404 Client Error for url: https://api.telegram.org/botsecret-token/sendMessage"
    )
    with (
        patch("httpx.AsyncClient.post", side_effect=error),
        caplog.at_level(logging.DEBUG),
    ):
        TelegramNotificationService("12345", "secret-token").send_message("Hi")

    assert "secret-token" not in caplog.text
    assert "bot***/sendMessage" in caplog.text
