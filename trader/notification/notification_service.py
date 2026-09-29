"""Notificações (Telegram) que nunca travam o loop do bot.

`send_message` não bloqueia nem levanta: dentro de um loop asyncio, o envio
vira uma task; fora de um loop, roda na hora (não há loop para travar).
`aclose()` espera os envios pendentes por um tempo limitado.
"""

import asyncio
import logging

import httpx

SEND_TIMEOUT_SECONDS = 10
CLOSE_TIMEOUT_SECONDS = 5


class NotificationService:
    def __init__(self):
        self.logger = logging.getLogger(self.__module__)

    def send_message(self, message: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


class NullNotificationService(NotificationService):
    pass


class TelegramNotificationService(NotificationService):
    def __init__(self, chat_id: str, token: str):
        super().__init__()

        self.chat_id = chat_id
        self.token = token

        self.url = f"https://api.telegram.org/bot{self.token}"
        # referências às tasks em voo (o loop só guarda referências fracas)
        self._pending: set[asyncio.Task] = set()

    def send_message(self, message: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._send(message))
            return
        task = loop.create_task(self._send(message))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, message: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self.url + "/sendMessage",
                    data={"chat_id": self.chat_id, "text": message},
                )
                response.raise_for_status()
        except Exception as e:
            # a mensagem de erro do httpx inclui a URL, que contém o token
            error = str(e).replace(self.token, "***")
            self.logger.warning(f"Erro ao enviar alerta Telegram: {error}")

    async def aclose(self) -> None:
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=CLOSE_TIMEOUT_SECONDS)
