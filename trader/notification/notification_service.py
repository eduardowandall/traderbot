import logging

import requests


class NotificationService:
    def __init__(self):
        self.logger = logging.getLogger(self.__module__)

    def send_message(self, message: str) -> None:
        pass


class NullNotificationService(NotificationService):
    def __init__(self):
        super().__init__()
        pass

    def send_message(self, message: str) -> None:
        pass


class TelegramNotificationService(NotificationService):
    def __init__(self, chat_id: str, token: str):
        super().__init__()

        self.chat_id = chat_id
        self.token = token

        self.url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, message: str) -> None:
        try:
            response = requests.post(
                self.url + "/sendMessage",
                data={"chat_id": self.chat_id, "text": message},
            )
            response.raise_for_status()
        except Exception as e:
            self.logger.warning("Erro ao enviar alerta Telegram:", exc_info=e)
