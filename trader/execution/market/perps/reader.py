"""`JupiterPerpsReader`: o estado da Jupiter Perps lido da rede (A10, só leitura).

Lê as contas do programa por JSON-RPC (`getMultipleAccounts`, Confirmed):
a pool JLP, a custody de um mercado (as taxas, a utilização e a curva de
empréstimo), o preço do oráculo (o feed agregado da Doves, que é o que a
Jupiter usa) e a posição de uma carteira (a PDA dela). Nada aqui monta
transação: as ordens de verdade são da A11.

O empréstimo segue o modelo "jump rate" da custody: a taxa anual (bps) sobe
em linha até a utilização-alvo e mais rápido depois; a por hora é a anual
dividida por 8760. A utilização é `locked / owned` dos ativos da custody.

Em produção só `borrow_bps_hour` é usado (o paper, A10); a pool, o preço do
oráculo e a posição ficam para a A11 (reconcile, ordens) e os testes live.
"""

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from solders.pubkey import Pubkey

from trader.execution.market.jupiter.client import HTTP_RETRY
from trader.execution.market.perps.idl import decode_account, discriminator
from trader.execution.models.perp import PerpTerms
from trader.shared.logging_config import error_text
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOLANA_MINTS

PERPS_PROGRAM = Pubkey.from_string("PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu")
JLP_POOL = Pubkey.from_string("5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq")
# a custody de cada token da pool, pelo mint (a decodificada confere o mint)
CUSTODIES: dict[str, Pubkey] = {
    SOLANA_MINTS.get_by_symbol(symbol).mint: Pubkey.from_string(address)
    for symbol, address in (
        ("SOL", "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz"),
        ("USDC", "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa"),
    )
}
# um vendido usa colateral em stablecoin: USDC (A8, D5)
SHORT_COLLATERAL = SOLANA_MINTS.get_by_symbol("USDC").mint
PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"
RPC_TIMEOUT_SECONDS = 20
# valores em USD da Jupiter Perps: 6 casas
USD_SCALE = Decimal(10**6)
RATE_POWER = Decimal(10**9)
HOURS_PER_YEAR = Decimal(8760)


class PerpsReadError(RuntimeError):
    """A conta não existe, o RPC falhou ou a custody não é do mercado."""


@dataclass(frozen=True)
class JumpRate:
    """A curva de empréstimo da custody: taxas anuais em bps, alvo em fração."""

    min_bps: Decimal
    max_bps: Decimal
    target_bps: Decimal
    target_utilization: Decimal

    def annual_bps(self, utilization: Decimal) -> Decimal:
        """A taxa anual na `utilization` (dois trechos lineares, quebra no alvo)."""
        target = self.target_utilization
        if utilization < target:
            slope = (self.target_bps - self.min_bps) / target
            return self.min_bps + slope * utilization
        slope = (self.max_bps - self.target_bps) / (1 - target)
        return self.target_bps + slope * (utilization - target)


@dataclass(frozen=True)
class CustodyState:
    mint: str
    decimals: int
    owned: int  # raw do token
    locked: int
    open_fee_bps: Decimal
    close_fee_bps: Decimal
    jump_rate: JumpRate
    oracle: Pubkey  # o feed agregado da Doves (`dovesAgOracle`)
    pyth_oracle: Pubkey  # o da Pyth (`oracle.oracleAccount`): o gatilho pede os dois

    @property
    def utilization(self) -> Decimal:
        return Decimal(self.locked) / Decimal(self.owned) if self.owned else Decimal(0)

    @property
    def borrow_bps_hour(self) -> Decimal:
        """Empréstimo por hora, em bps do tamanho da posição."""
        return self.jump_rate.annual_bps(self.utilization) / HOURS_PER_YEAR


@dataclass(frozen=True)
class OraclePrice:
    price: Decimal  # USD
    timestamp: datetime  # UTC


@dataclass(frozen=True)
class VenuePosition:
    """Uma posição aberta na Jupiter Perps (valores em USD)."""

    address: Pubkey
    direction: Direction
    price: Decimal  # de entrada
    size_usd: Decimal
    collateral_usd: Decimal
    opened_at: datetime


def custody_of(mint: str) -> Pubkey:
    custody = CUSTODIES.get(mint)
    if custody is None:
        raise PerpsReadError(f"sem custody de {SOLANA_MINTS.symbol_of(mint)}")
    return custody


def collateral_mint(terms: PerpTerms) -> str:
    """O colateral da posição: o próprio token num comprado, USDC num vendido."""
    return terms.market_mint if terms.direction == Direction.LONG else SHORT_COLLATERAL


def position_address(owner: Pubkey, terms: PerpTerms) -> Pubkey:
    """A PDA da posição: uma por carteira, mercado, colateral e lado."""
    long = terms.direction == Direction.LONG
    collateral = collateral_mint(terms)
    seeds = [
        b"position",
        bytes(owner),
        bytes(JLP_POOL),
        bytes(custody_of(terms.market_mint)),
        bytes(custody_of(collateral)),
        bytes([1 if long else 2]),
    ]
    return Pubkey.find_program_address(seeds, PERPS_PROGRAM)[0]


class JupiterPerpsReader:
    def __init__(
        self, rpc_url: str | None = None, client: httpx.AsyncClient | None = None
    ):
        # o RPC do dono se houver (só leitura); senão o público da Solana
        self.rpc_url = rpc_url or os.getenv("HELIUS_RPC_URL") or PUBLIC_RPC_URL
        self.client = client or httpx.AsyncClient(timeout=RPC_TIMEOUT_SECONDS)
        # o feed de preço de cada custody (um endereço que não muda)
        self._oracles: dict[str, Pubkey] = {}

    def __repr__(self):
        # a URL do dono tem a chave da Helius: nunca no repr nem nos logs
        return f"{self.__class__.__name__}()"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def accounts(self, addresses: list[Pubkey]) -> list[bytes | None]:
        """Os dados das contas (None: não existe), lidas juntas, em Confirmed."""
        values = await self._multiple(addresses)
        return [None if v is None else base64.b64decode(v["data"][0]) for v in values]

    async def lamports(self, addresses: list[Pubkey]) -> list[int | None]:
        """Os lamports das contas (None: não existe), sem os dados (A11c)."""
        values = await self._multiple(addresses, {"offset": 0, "length": 0})
        return [None if v is None else int(v["lamports"]) for v in values]

    async def _multiple(
        self, addresses: list[Pubkey], data_slice: dict | None = None
    ) -> list[dict | None]:
        options: dict[str, Any] = {"encoding": "base64", "commitment": "confirmed"}
        if data_slice is not None:
            options["dataSlice"] = data_slice
        result = await self._rpc(
            "getMultipleAccounts", [[str(a) for a in addresses], options]
        )
        return result["value"]

    async def program_accounts(
        self, kind: str, matches: list[tuple[int, bytes]]
    ) -> list[Pubkey]:
        """As contas `kind` do programa cujos bytes em cada offset batem.

        Só os endereços (`getProgramAccounts` sem dados), e só para buscas
        avulsas (testes, achar posições órfãs): varre o programa inteiro.
        """
        filters = [_memcmp(o, b) for o, b in [(0, discriminator(kind)), *matches]]
        options = {
            "encoding": "base64",
            "commitment": "confirmed",
            "filters": filters,
            "dataSlice": {"offset": 0, "length": 0},
        }
        found = await self._rpc("getProgramAccounts", [str(PERPS_PROGRAM), options])
        return [Pubkey.from_string(a["pubkey"]) for a in found]

    async def _rpc(self, method: str, params: list) -> Any:
        """Uma chamada JSON-RPC; qualquer falha (rede, HTTP, erro do RPC) é
        `PerpsReadError` com o motivo."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            return await self._post(body)
        except (httpx.HTTPError, KeyError, ValueError) as ex:
            raise PerpsReadError(f"RPC falhou: {error_text(ex)}") from ex

    @HTTP_RETRY  # o RPC público devolve 429 em rajadas: espera e tenta de novo
    async def _post(self, body: dict) -> Any:
        response = await self.client.post(self.rpc_url, json=body)
        response.raise_for_status()
        reply = response.json()
        if "error" in reply:
            raise ValueError(reply["error"].get("message", reply["error"]))
        return reply["result"]

    async def _one(self, address: Pubkey, kind: str) -> dict:
        [data] = await self.accounts([address])
        if data is None:
            raise PerpsReadError(f"{kind} {address} não existe")
        return decode_account(kind, data)

    async def pool(self) -> dict:
        return await self._one(JLP_POOL, "Pool")

    async def custody(self, mint: str) -> CustodyState:
        state = custody_state(await self._one(custody_of(mint), "Custody"), mint)
        self._oracles[mint] = state.oracle
        return state

    async def simulate(self, transaction: bytes) -> tuple[int, list[str]]:
        """Simula uma transação (sem assinatura, blockhash da hora): unidades e logs.

        Só lê (A11a): nada é enviado. Uma falha do programa é `PerpsReadError`
        com a última linha do log, que diz o porquê.
        """
        options = {
            "encoding": "base64",
            "sigVerify": False,
            "replaceRecentBlockhash": True,
            "commitment": "confirmed",
        }
        params = [base64.b64encode(transaction).decode(), options]
        value = (await self._rpc("simulateTransaction", params))["value"]
        logs = value.get("logs") or []
        if value.get("err") is not None:
            last = logs[-1] if logs else ""
            raise PerpsReadError(f"simulação falhou: {value['err']} ({last})")
        return value.get("unitsConsumed") or 0, logs

    async def priority_fee(self, accounts: list[Pubkey]) -> int:
        """O preço por unidade (micro-lamports) que as transações recentes que
        tocam `accounts` pagaram: o percentil 75 (0 sem dados)."""
        result = await self._rpc(
            "getRecentPrioritizationFees", [[str(a) for a in accounts]]
        )
        fees = sorted(int(r["prioritizationFee"]) for r in result)
        return fees[len(fees) * 3 // 4] if fees else 0

    async def token_amount(self, account: Pubkey) -> int:
        """O saldo (raw) de uma conta de token SPL; 0 se ela não existe."""
        [data] = await self.accounts([account])
        return 0 if data is None else int.from_bytes(data[64:72], "little")

    async def last_payout(self, address: Pubkey, owner: Pubkey, mint: str) -> int:
        """Quanto de `mint` (raw) a última transação em `address` deu a `owner`.

        Para uma saída que o venue fez sozinho (o stop disparou, uma
        liquidação): a do keeper é a última na conta da posição; 0 se nada
        voltou (liquidada) ou se não há transação.
        """
        found = await self._rpc(
            "getSignaturesForAddress",
            [str(address), {"limit": 1, "commitment": "confirmed"}],
        )
        if not found:
            return 0
        options = {
            "encoding": "json",
            "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0,
        }
        signature = found[0]["signature"]
        tx = await self._rpc("getTransaction", [signature, options])
        if tx is None:  # ainda não indexada: a próxima varredura lê de novo
            raise PerpsReadError(f"transação {signature} indisponível")
        meta = tx.get("meta") or {}
        return _balance(meta, "postTokenBalances", owner, mint) - _balance(
            meta, "preTokenBalances", owner, mint
        )

    async def borrow_bps_hour(self, mint: str) -> Decimal:
        return (await self.custody(mint)).borrow_bps_hour

    async def oracle_price(self, mint: str) -> OraclePrice:
        """O preço que a Jupiter usa: o feed agregado da Doves da custody."""
        if mint not in self._oracles:
            await self.custody(mint)
        feed = await self._one(self._oracles[mint], "AgPriceFeed")
        return OraclePrice(
            Decimal(feed["price"]).scaleb(feed["expo"]),
            datetime.fromtimestamp(feed["timestamp"], UTC),
        )

    async def position(self, owner: Pubkey, terms: PerpTerms) -> VenuePosition | None:
        """A posição da carteira nesse mercado e lado; None se não há."""
        address = position_address(owner, terms)
        [data] = await self.accounts([address])
        return open_position(address, data)


def custody_state(raw: dict, mint: str) -> CustodyState:
    if str(raw["mint"]) != mint:
        raise PerpsReadError(f"a custody é de {raw['mint']}, não de {mint}")
    assets, jump = raw["assets"], raw["jumpRateState"]
    return CustodyState(
        mint=mint,
        decimals=raw["decimals"],
        owned=assets["owned"],
        locked=assets["locked"],
        open_fee_bps=Decimal(raw["increasePositionBps"]),
        close_fee_bps=Decimal(raw["decreasePositionBps"]),
        jump_rate=JumpRate(
            min_bps=Decimal(jump["minRateBps"]),
            max_bps=Decimal(jump["maxRateBps"]),
            target_bps=Decimal(jump["targetRateBps"]),
            target_utilization=Decimal(jump["targetUtilizationRate"]) / RATE_POWER,
        ),
        oracle=raw["dovesAgOracle"],
        pyth_oracle=raw["oracle"]["oracleAccount"],
    )


def open_position(address: Pubkey, data: bytes | None) -> VenuePosition | None:
    """A posição de uma conta lida; None se não há.

    A conta fica criada (com tamanho 0) depois de fechada: tamanho 0 é None.
    """
    if data is None:
        return None
    raw = decode_account("Position", data)
    return venue_position(address, raw) if raw["sizeUsd"] else None


def venue_position(address: Pubkey, raw: dict) -> VenuePosition:
    return VenuePosition(
        address=address,
        direction=Direction.LONG if raw["side"] == "Long" else Direction.SHORT,
        price=Decimal(raw["price"]) / USD_SCALE,
        size_usd=Decimal(raw["sizeUsd"]) / USD_SCALE,
        collateral_usd=Decimal(raw["collateralUsd"]) / USD_SCALE,
        opened_at=datetime.fromtimestamp(raw["openTime"], UTC),
    )


def _memcmp(offset: int, raw: bytes) -> dict:
    """Um filtro `memcmp` do RPC (bytes em base64)."""
    return {
        "memcmp": {
            "offset": offset,
            "bytes": base64.b64encode(raw).decode(),
            "encoding": "base64",
        }
    }


def _balance(meta: dict, key: str, owner: Pubkey, mint: str) -> int:
    """O saldo de `mint` de `owner` numa lista de saldos de token da transação."""
    return sum(
        int(b["uiTokenAmount"]["amount"])
        for b in meta.get(key) or []
        if b.get("owner") == str(owner) and b.get("mint") == mint
    )
