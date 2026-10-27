"""Avisos de queda de rede (A28): um Telegram quando começa, um quando passa.

Uma noite real (A11b, G6) ficou 17 min sem preço e sem a conferência das
perps, com uma posição aberta, e ninguém soube: o log tinha um aviso a cada
tentativa. `Outage` acompanha uma fonte (os preços do hub, a varredura das
perps) e manda **uma** mensagem depois de `after` segundos fora com alguma
posição aberta, e **uma** quando ela volta. Sem posição aberta, não avisa
(nada está em risco); a contagem continua, para avisar se uma abrir.
"""

import logging
import time
from collections.abc import Callable

from trader.shared.notification.notification_service import Notifier

logger = logging.getLogger(__name__)

ALERT_AFTER_SECONDS = 120.0


class Outage:
    def __init__(
        self,
        what: str,
        notifier: Notifier,
        after: float = ALERT_AFTER_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        # o que caiu, para a mensagem ("serve real: preços")
        self.what = what
        self.notifier = notifier
        self.after = after
        self.monotonic = monotonic
        self._since: float | None = None  # a 1ª falha desta queda
        self._alerted = False

    def failing(self, with_position: bool, detail: str) -> None:
        """A fonte falhou agora; avisa uma vez, passados `after` segundos."""
        now = self.monotonic()
        if self._since is None:
            self._since = now
        down = now - self._since
        if self._alerted or not with_position or down < self.after:
            return
        self._alerted = True
        message = f"[!] {self.what} fora há {down:.0f}s com posição aberta: {detail}"
        logger.error(message)
        self.notifier.send_message(message)

    def ok(self) -> None:
        """A fonte respondeu: se a queda foi avisada, avisa a volta."""
        if self._since is not None and self._alerted:
            down = self.monotonic() - self._since
            message = f"{self.what} de volta depois de {down:.0f}s"
            logger.warning(message)
            self.notifier.send_message(message)
        self._since = None
        self._alerted = False
