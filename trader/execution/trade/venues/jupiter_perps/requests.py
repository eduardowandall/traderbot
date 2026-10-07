"""Os pedidos da Jupiter Perps (A11a): abrir, a ordem de stop no venue e fechar.

Na Jupiter Perps quem opera não mexe na posição direto: cria um pedido
(`PositionRequest`) assinado pela carteira, e um keeper da Jupiter o executa
segundos depois (ou recusa). Aqui só se montam as instruções e a transação,
sem assinar: o envio é da A11b, só no `serve real`.

- `open_request`: `createIncreasePositionMarketRequest`. O colateral entra em
  USDC (o token de cotação do bucket); num comprado o keeper troca por SOL
  (`jupiter_minimum_out` é o mínimo de SOL dessa troca).
- `stop_request`: `createDecreasePositionRequest2` com gatilho, a posição
  inteira, USDC de volta: a ordem de stop que fica no venue (D7).
- `close_request`: `createDecreasePositionMarketRequest`, a posição inteira.

O preço-limite (`priceSlippage`) protege a execução: um comprado abre por no
máximo preço x (1 + slippage) e fecha por no mínimo preço x (1 - slippage);
um vendido, o contrário. Valores em USD e preços com 6 casas.

O contador do pedido vem da chave de idempotência (D8): a mesma intenção mira
sempre a mesma conta de pedido. Enquanto o pedido existe, um reenvio falha;
depois que o keeper o executa a conta some, então a A11b lê a posição antes
de reenviar (D8). Uma posição por carteira, mercado e lado: a compra de um
bucket é recusada se outro já a tem aberta (`PerpVenue.has_position`), então
fechar a posição inteira nunca fecha a de outro bucket.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM
from solders.transaction import VersionedTransaction
from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

from trader.execution.market.perps.encode import (
    encode_instruction,
    instruction_accounts,
)
from trader.execution.market.perps.reader import (
    CUSTODIES,
    JLP_POOL,
    PERPS_PROGRAM,
    SHORT_COLLATERAL,
    USD_SCALE,
    CustodyState,
    collateral_mint,
    custody_of,
    position_address,
)
from trader.execution.models.perp import PerpTerms
from trader.shared.models.direction import Direction

# o simulado usa ~95 mil; o teto dá folga sem pagar a mais (a taxa é por unidade)
COMPUTE_UNIT_LIMIT = 200_000
MICRO_PER_LAMPORT = 1_000_000

OPEN = "createIncreasePositionMarketRequest"
STOP = "createDecreasePositionRequest2"
CLOSE = "createDecreasePositionMarketRequest"


def _pda(*seeds: bytes) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), PERPS_PROGRAM)[0]


PERPETUALS = _pda(b"perpetuals")
EVENT_AUTHORITY = _pda(b"__event_authority")


USDC = Pubkey.from_string(SHORT_COLLATERAL)


def token_account(owner: Pubkey, mint: Pubkey = USDC) -> Pubkey:
    """A conta associada (ATA) de `owner` para `mint` (o dono pode ser uma PDA)."""
    return get_associated_token_address(owner, mint, TOKEN_PROGRAM_ID)


def request_counter(idempotency_key: str) -> int:
    """O contador do pedido: os 8 primeiros bytes do sha256 da chave (D8)."""
    return int.from_bytes(
        hashlib.sha256(idempotency_key.encode()).digest()[:8], "little"
    )


def request_address(position: Pubkey, counter: int, increase: bool) -> Pubkey:
    change = bytes([1 if increase else 2])
    return _pda(
        b"position_request", bytes(position), counter.to_bytes(8, "little"), change
    )


@dataclass(frozen=True)
class PerpRequest:
    """Um pedido montado: a instrução e a conta que ele cria."""

    instruction: Instruction
    request: Pubkey
    position: Pubkey
    counter: int


def _usd(value: Decimal) -> int:
    return int(value * USD_SCALE)


def _limit(price: Decimal, slippage: Decimal, pay_more: bool) -> int:
    """O preço-limite: acima do preço quando se aceita pagar mais, abaixo senão."""
    return _usd(price * (1 + slippage if pay_more else 1 - slippage))


def _instruction(name: str, args: dict, accounts: dict[str, Pubkey]) -> Instruction:
    """A instrução `name` com as contas pela ordem da IDL (opcional ausente: o
    próprio programa, a convenção do Anchor)."""
    metas = [
        AccountMeta(accounts.get(a["name"], PERPS_PROGRAM), a["isSigner"], a["isMut"])
        for a in instruction_accounts(name)
    ]
    return Instruction(PERPS_PROGRAM, encode_instruction(name, {"params": args}), metas)


def _common(owner: Pubkey, terms: PerpTerms, position: Pubkey, request: Pubkey) -> dict:
    """As contas que os três pedidos têm em comum (o USDC entra e sai)."""
    return {
        "owner": owner,
        "perpetuals": PERPETUALS,
        "pool": JLP_POOL,
        "position": position,
        "positionRequest": request,
        "positionRequestAta": token_account(request),
        "custody": custody_of(terms.market_mint),
        "collateralCustody": CUSTODIES[collateral_mint(terms)],
        "tokenProgram": TOKEN_PROGRAM_ID,
        "associatedTokenProgram": ASSOCIATED_TOKEN_PROGRAM_ID,
        "systemProgram": SYSTEM_PROGRAM,
        "eventAuthority": EVENT_AUTHORITY,
        "program": PERPS_PROGRAM,
    }


def open_request(
    owner: Pubkey,
    terms: PerpTerms,
    collateral_raw: int,
    size_usd: Decimal,
    price: Decimal,
    slippage: Decimal,
    idempotency_key: str,
    jupiter_minimum_out: int | None = None,
) -> PerpRequest:
    """Abre (ou aumenta) a posição com `collateral_raw` de USDC e `size_usd`."""
    long = terms.direction == Direction.LONG
    if long and not jupiter_minimum_out:
        raise ValueError("um comprado em USDC precisa do mínimo da troca por SOL")
    position = position_address(owner, terms)
    counter = request_counter(idempotency_key)
    request = request_address(position, counter, increase=True)
    accounts = _common(owner, terms, position, request) | {
        "fundingAccount": token_account(owner),
        "inputMint": USDC,
    }
    args = {
        "sizeUsdDelta": _usd(size_usd),
        "collateralTokenDelta": collateral_raw,
        "side": "Long" if long else "Short",
        # abrir comprado é comprar: aceita pagar até o limite acima do preço
        "priceSlippage": _limit(price, slippage, pay_more=long),
        "jupiterMinimumOut": jupiter_minimum_out if long else None,
        "counter": counter,
    }
    return PerpRequest(_instruction(OPEN, args, accounts), request, position, counter)


def _decrease_accounts(
    owner: Pubkey, terms: PerpTerms, position: Pubkey, request: Pubkey
) -> dict:
    return _common(owner, terms, position, request) | {
        "receivingAccount": token_account(owner),
        "desiredMint": USDC,
    }


def close_request(
    owner: Pubkey,
    terms: PerpTerms,
    price: Decimal,
    slippage: Decimal,
    idempotency_key: str,
) -> PerpRequest:
    """Fecha a posição inteira a mercado; o colateral volta em USDC."""
    position = position_address(owner, terms)
    counter = request_counter(idempotency_key)
    request = request_address(position, counter, increase=False)
    args = {
        "collateralUsdDelta": 0,
        "sizeUsdDelta": 0,
        # fechar um vendido é recomprar: aceita pagar até o limite acima
        "priceSlippage": _limit(
            price, slippage, pay_more=terms.direction == Direction.SHORT
        ),
        "jupiterMinimumOut": None,
        "entirePosition": True,
        "counter": counter,
    }
    accounts = _decrease_accounts(owner, terms, position, request)
    return PerpRequest(_instruction(CLOSE, args, accounts), request, position, counter)


def stop_request(
    owner: Pubkey,
    terms: PerpTerms,
    stop_price: Decimal,
    custody: CustodyState,
    idempotency_key: str,
) -> PerpRequest:
    """A ordem de stop no venue (D7): fecha tudo quando o preço passa do stop.

    Num vendido o stop fica acima do preço (dispara subindo); num comprado,
    abaixo. O keeper confere o gatilho pelos oráculos da custody.
    """
    position = position_address(owner, terms)
    counter = request_counter(idempotency_key)
    request = request_address(position, counter, increase=False)
    args = {
        "collateralUsdDelta": 0,
        "sizeUsdDelta": 0,
        "requestType": "Trigger",
        "priceSlippage": None,
        "jupiterMinimumOut": None,
        "triggerPrice": _usd(stop_price),
        "triggerAboveThreshold": terms.direction == Direction.SHORT,
        "entirePosition": True,
        "counter": counter,
    }
    accounts = _decrease_accounts(owner, terms, position, request) | {
        "custodyDovesPriceAccount": custody.oracle,
        "custodyPythnetPriceAccount": custody.pyth_oracle,
    }
    return PerpRequest(_instruction(STOP, args, accounts), request, position, counter)


def budgeted(request: PerpRequest, limit: int, micro_price: int) -> list[Instruction]:
    """O pedido com o limite de unidades e o preço por unidade na frente."""
    return [
        set_compute_unit_limit(limit),
        set_compute_unit_price(micro_price),
        request.instruction,
    ]


def build_transaction(
    owner: Pubkey, request: PerpRequest, priority_fee_lamports: int
) -> VersionedTransaction:
    """A transação v0 do pedido, sem assinatura (a A11b assina no `serve`).

    O preço por unidade sai do teto da política: no máximo
    `priority_fee_lamports` de prioridade com o limite de unidades inteiro.
    """
    micro = priority_fee_lamports * MICRO_PER_LAMPORT // COMPUTE_UNIT_LIMIT
    instructions = budgeted(request, COMPUTE_UNIT_LIMIT, micro)
    # o blockhash é o da hora na simulação; a A11b compila de novo ao assinar
    message = MessageV0.try_compile(owner, instructions, [], Hash.default())
    return VersionedTransaction.populate(message, [Signature.default()])
