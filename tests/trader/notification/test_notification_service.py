import logging
from unittest.mock import patch

from trader.notification.notification_service import (
    NotificationService,
    NullNotificationService,
    TelegramNotificationService,
)


def test_null_notification_service_is_noop():
    service = NullNotificationService()
    assert isinstance(service, NotificationService)
    assert service.send_message("ignored") is None


def test_telegram_init():
    service = TelegramNotificationService("12345", "token123")
    assert service.chat_id == "12345"
    assert service.token == "token123"
    assert service.url == "https://api.telegram.org/bottoken123"


@patch("requests.post")
def test_telegram_send_message_success(mock_post):
    service = TelegramNotificationService("12345", "token123")
    service.send_message("Hello World")
    mock_post.assert_called_once_with(
        "https://api.telegram.org/bottoken123/sendMessage",
        data={"chat_id": "12345", "text": "Hello World"},
    )


@patch("requests.post")
def test_telegram_send_message_handles_exception(mock_post, caplog):
    mock_post.side_effect = Exception("Network error")
    service = TelegramNotificationService("12345", "token123")

    with caplog.at_level(logging.INFO):
        service.send_message("Hello World")
    assert "Erro ao enviar alerta Telegram:" in caplog.text
