"""O serviço de trading do trade-runner: os buckets e as ordens deles.

`TradeService` (`service.py`) abre os buckets, limita o que cada um gasta e
executa as ordens; `rent.py` fecha a conta de token de um bucket encerrado.
O lado da estratégia conhece só o protocolo (`trader/shared/trading_service`);
o `serve` o atende por socket (`trader/execution/runner.py`) e o backtest
chama o serviço direto.
"""
