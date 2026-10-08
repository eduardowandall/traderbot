"""Os tipos de evento que a execução grava no ledger (tabela `events`).

Os nomes ficam no arquivo do ledger para sempre: renomear um quebra a
leitura de ledgers antigos e os scripts (`ledger_dump.py`). Cada mudança de
estado de uma intenção grava também `intent_<status>` (`intent_event`).
"""

from trader.execution.models.intent import IntentStatus


def intent_event(status: IntentStatus) -> str:
    """O evento de uma intenção que passou a `status` (`intent_executed`...)."""
    return f"intent_{status}"


# intenções (A3, A8)
INTENT_SENT = "intent_sent"  # gravado antes do envio
INTENT_RESOLVED = "intent_resolved"
INTENT_EXTERNAL = "intent_external"  # uma perna que o venue fez sem nós
ORDER_RECORDED = "order_recorded"  # a ordem e o PnL de uma intenção executada

# custos fora das intenções
FAILED_TX_FEE = "failed_tx_fee"
RENT_REFUND_SENT = "rent_refund_sent"  # gravado antes do envio (A15)
RENT_REFUND = "rent_refund"
POSITION_LEFTOVER = "position_leftover"

# buckets
BUCKET_OPENED = "bucket_opened"
BUCKET_RETIRED = "bucket_retired"
BUCKET_MAX_LOSS = "bucket_max_loss"
RECONCILE_MISMATCH = "reconcile_mismatch"
SELL_SHORTFALL = "sell_shortfall"

# perps (A8, A11b)
PERP_MISMATCH = "perp_mismatch"
PERP_LIQUIDATED = "perp_liquidated"
PERP_VENUE_EXIT = "perp_venue_exit"
PERP_STOP_SENT = "perp_stop_sent"
PERP_STOP_PLACED = "perp_stop_placed"
PERP_STOP_FAILED = "perp_stop_failed"
PERP_STOP_LEFT = "perp_stop_left"

# o relatório diário já enviado (um por dia UTC)
DAILY_REPORT = "daily_report"
