from decimal import Decimal

from solders.pubkey import Pubkey


class Mint:
    __slots__ = ("mint", "symbol", "decimals", "is_usd_stable", "_pubkey", "_scale")

    def __init__(
        self, mint: str, symbol: str, decimals: int, is_usd_stable: bool = False
    ):
        self.mint = mint
        self.symbol = symbol
        self.decimals = decimals
        # stablecoin atrelada ao dólar: preço em unidades dela ~= preço em USD
        self.is_usd_stable = is_usd_stable
        # calculados uma vez: usados a cada tick/ordem
        self._pubkey = Pubkey.from_string(mint)
        self._scale = Decimal(10) ** decimals

    @property
    def pubkey(self) -> Pubkey:
        return self._pubkey

    def ui_to_raw(self, ui_amount: Decimal | int | str) -> int:
        """
        Converte valor em UI (ex: 1.23 USDC) para raw (int)
        """
        return int(Decimal(ui_amount) * self._scale)

    def raw_to_ui(self, raw_amount: int | Decimal) -> Decimal:
        """
        Converte valor raw (int) para UI
        """
        return Decimal(raw_amount) / self._scale

    def __repr__(self) -> str:
        return f"{self.symbol} ({self.mint[:6]}..)"


class SolanaMints(dict[str, Mint]):
    def __init__(self, mints: list[Mint]):
        super().__init__({m.mint: m for m in mints})
        self._by_symbol = {m.symbol: m for m in mints}

    def get_by_symbol(self, symbol: str) -> Mint:
        try:
            return self._by_symbol[symbol]
        except KeyError:
            raise ValueError(
                f"{symbol=} não existe na lista de mints salvas."
            ) from None

    def get_pair(self, symbol: str) -> tuple[Mint, Mint]:
        """'SOL-USDC' -> (SOL, USDC): (token comprado, token gasto)."""
        output, sep, input_ = symbol.partition("-")
        if not sep:
            raise ValueError(f"par inválido {symbol!r}; use SAIDA-ENTRADA")
        return self.get_by_symbol(output), self.get_by_symbol(input_)

    def symbol_of(self, mint: Pubkey | str) -> str:
        """Símbolo do mint, ou o próprio endereço se for desconhecido."""
        info = self.get(mint)
        return info.symbol if info else self._normalize_key(mint)

    def decimals(self, mint: str) -> int:
        return self[mint].decimals

    def ui_to_raw(self, mint: str, ui_amount: Decimal | int | str) -> int:
        return self[mint].ui_to_raw(ui_amount)

    def raw_to_ui(self, mint: str, raw_amount: int) -> Decimal:
        return self[mint].raw_to_ui(raw_amount)

    @staticmethod
    def _normalize_key(key: Pubkey | str) -> str:
        if isinstance(key, Pubkey):
            return str(key)
        return key

    # --- overrides de dict ---
    def __getitem__(self, key: Pubkey | str) -> Mint:
        return super().__getitem__(self._normalize_key(key))

    def __contains__(self, key: Pubkey | str) -> bool:  # ty:ignore[invalid-method-override]
        return super().__contains__(self._normalize_key(key))

    def get(self, key: Pubkey | str, default=None):
        return super().get(self._normalize_key(key), default)


SOLANA_MINTS = SolanaMints(
    [
        Mint("So11111111111111111111111111111111111111112", "SOL", 9),
        Mint("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC", 6, True),
        Mint("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "USDT", 6, True),
        Mint("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "BONK", 5),
        Mint("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "JUP", 6),
        Mint("pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn", "PUMP", 6),
        Mint("2Dyzu65QA9zdX1UeE7Gx71k7fiwyUK6sZdrvJ7auq5wm", "TURBO", 8),
        Mint("C29ebrgYjYoJPMGPnPSGY1q3mMGk4iDSqnQeQQA7moon", "NOBODY", 9),
    ]
)
