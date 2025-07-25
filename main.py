import os

from dotenv import load_dotenv

from trader.account import Account
from trader.api import FakeMercadoBitcoinPrivateAPI, MercadoBitcoinPublicAPI
from trader.bot import TradingBot
from trader.trading_strategy import IterationStrategy


def main():
    # Carregar variáveis do arquivo .env
    load_dotenv()

    # Configurar credenciais (use variáveis de ambiente)
    api_key = os.getenv("MB_API_KEY", "fake_key")
    api_secret = os.getenv("MB_API_SECRET", "fake_secret")

    if not api_key or not api_secret:
        print("Configure as variáveis MB_API_KEY e MB_API_SECRET")
        return

    # Inicializar API
    account_api = FakeMercadoBitcoinPrivateAPI(api_key, api_secret)
    public_api = MercadoBitcoinPublicAPI()

    # Configurar estratégia
    strategy = IterationStrategy(sell_on_iteration=10)

    # Configurar conta
    account = Account(account_api, "BTC-BRL")

    # Criar e executar bot
    bot = TradingBot(public_api, strategy, account)

    try:
        bot.run(interval=30)
    except KeyboardInterrupt:
        bot.stop()


if __name__ == "__main__":
    main()
