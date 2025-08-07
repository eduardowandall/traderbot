import sys

from dotenv import load_dotenv

from report.report_method import get_report_cls
from trader import NotImplementedStrategy, get_strategy_cls
from trader.account import Account
from trader.api import FakeMercadoBitcoinPrivateAPI, MercadoBitcoinPublicAPI
from trader.api.private_api import MercadoBitcoinPrivateAPI
from trader.backtesting.bot import BacktestingBot
from trader.trading.bot import TradingBot


def main(
    currency: str,
    strategy: str,
    interval: int,
    fake: bool = False,
    backtest: bool = False,
    report: str = "null",
    api_key: str | None = None,
    api_secret: str | None = None,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    **strategy_args,
):
    """
    Função principal do trading bot.

    Args:
        --help              Mostra mensagem de ajuda
        --currency          Par de moedas para negociar (ex: 'BTC-BRL')
        --strategy          Nome da estratégia de trading a ser utilizada
        --interval          Intervalo em segundos entre verificações de mercado
        --fake              Se True, usa API fake para simulação; se False, usa API real
        --backtest          Se True, executa backtesting em vez de trading real
        --report            Methodo de report:
                              - 'null' (padrão): Não gera report
                              - 'csv': Salva dados em CSV na pasta report/data/
        --api_key           Chave da API do Mercado Bitcoin (obrigatória se fake=False)
        --api_secret        Segredo da API do Mercado Bitcoin (obrigatório se fake=False)
        --start_datetime        Data de início do backtesting (obrigatório se backtest=True)
        --end_datetime          Data de fim do backtesting (obrigatório se backtest=True)
        **strategy_args     Argumentos específicos da estratégia selecionada (execute main.py --strategy=<nome> --help para mais informações)

    Exemplos:
        # Execução básica sem salvar dados:
        python main.py --strategy=iteration --currency=BTC-BRL --interval=1 --fake

        # Execução salvando dados em CSV:
        python main.py --strategy=iteration --currency=BTC-BRL --interval=1 --fake --report=file
    """
    # Configurar credenciais (use variáveis de ambiente)
    if fake or backtest:
        account_api = FakeMercadoBitcoinPrivateAPI()
    else:
        if not api_key or not api_secret:
            print("Argumentos api_key e api_secret são obrigatórios se fake=False")
            return
        account_api = MercadoBitcoinPrivateAPI(api_key, api_secret)

    # Inicializar API pública
    public_api = MercadoBitcoinPublicAPI()

    # Configurar estratégia
    try:
        strategy_cls = get_strategy_cls(strategy)
    except NotImplementedStrategy:
        print(f"Estratégia {strategy} não suportada")
        return

    try:
        strategy_obj = strategy_cls(**strategy_args)
    except ValueError as e:
        print(f"Erro ao configurar estratégia: {e}")
        return
    # Configurar conta
    account = Account(account_api, currency)

    # Configurar persistência
    try:
        report_cls = get_report_cls(report)
        report_obj = report_cls(currency)
    except ValueError as e:
        print(f"Erro na configuração de persistência: {e}")
        return

    if backtest:
        bot = BacktestingBot(
            public_api, strategy_obj, report_obj, account, start_datetime, end_datetime
        )
    else:
        bot = TradingBot(public_api, strategy_obj, report_obj, account)

    try:
        bot.run(interval=int(interval))
    except KeyboardInterrupt:
        bot.stop()


def parse_kwargs(argv):
    kwargs = {}

    for arg in argv:
        if arg.startswith("--"):
            key_value = arg[2:].split("=", 1)
            if len(key_value) == 2:
                key, value = key_value
                kwargs[key] = value
            else:
                kwargs[key_value[0]] = True  # flag booleana

    return kwargs


def _help(strategy: str | None = None):
    if strategy:
        try:
            strategy_cls = get_strategy_cls(strategy)
        except NotImplementedStrategy:
            print(f"Estratégia {strategy} não suportada")
            return
        return print(strategy_cls.__doc__)
    else:
        return print(main.__doc__)


if __name__ == "__main__":
    kwargs = parse_kwargs(sys.argv[1:])
    load_dotenv()
    print("args = ", kwargs)
    if kwargs.get("help"):
        _help(kwargs.get("strategy"))
    else:
        # Separar argumentos específicos do main dos argumentos da estratégia
        main_args = {
            "currency": kwargs.pop("currency", None),
            "strategy": kwargs.pop("strategy", None),
            "interval": kwargs.pop("interval", None),
            "fake": kwargs.pop("fake", False),
            "backtest": kwargs.pop("backtest", False),
            "report": kwargs.pop("report", "null"),
            "api_key": kwargs.pop("api_key", None),
            "api_secret": kwargs.pop("api_secret", None),
            "start_datetime": kwargs.pop("start-date", None),
            "end_datetime": kwargs.pop("end-date", None),
        }
        # Os argumentos restantes são para a estratégia
        main_args.update(kwargs)  # strategy_args
        main(**main_args)
