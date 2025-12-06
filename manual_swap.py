import base64
import os
import time
from decimal import Decimal

import requests
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token.instructions import (
    TOKEN_2022_PROGRAM_ID,  # type: ignore
    TOKEN_PROGRAM_ID,  # type: ignore
)

from trader.models.account_data import AccountBalanceData
from trader.providers.jupiter.jupiter_public_api import (
    SOLANA_TOKENS,
    SOLANA_TOKENS_BY_MINT,
    SOLANA_TOKENS_DECIMALS,
)

wallet_public_key = os.getenv("WALLET_KEY")
assert wallet_public_key, "Chave pública da wallet não definida"
wallet = Pubkey.from_string(wallet_public_key)
rpc_url = os.getenv("HELIUS_RPC_URL")
client = Client(rpc_url)
private_key = os.getenv("SOLANA_PRIVATE_KEY")
assert private_key, "Chave privada da wallet não definida"
keypair = Keypair.from_base58_string(private_key)


def do_swap(
    symbol_in: str,
    symbol_out: str,
    quantity: float,
    slippage_bps: int = 50,
):
    mint_in = SOLANA_TOKENS[symbol_in]
    mint_out = SOLANA_TOKENS[symbol_out]

    decimals = SOLANA_TOKENS_DECIMALS[symbol_out]
    amount_in = int(Decimal(quantity) * (10**decimals))

    print("→ Criando rota na Jupiter...")
    quote = requests.get(
        "https://lite-api.jup.ag/swap/v1/quote",
        params={
            "inputMint": mint_in,
            "outputMint": mint_out,
            "amount": amount_in,
            "slippageBps": slippage_bps,
        },
    ).json()

    if not quote.get("routePlan"):
        raise Exception("Nenhuma rota encontrada!")

    print("✓ Rota encontrada.")

    print("→ Gerando transação de swap...")
    swap_tx = requests.post(
        "https://lite-api.jup.ag/swap/v1/swap",
        json={
            "quoteResponse": quote,
            "userPublicKey": wallet_public_key,
        },
    ).json()
    raw_tx = base64.b64decode(swap_tx["swapTransaction"])

    # ---------- desserializar ----------
    tx = VersionedTransaction.from_bytes(raw_tx)

    # ---------- assinar ----------
    print("→ Assinando transação...")
    latest = client.get_latest_blockhash()
    blockhash = latest.value.blockhash
    message = tx.message
    message = MessageV0(
        header=tx.message.header,
        account_keys=tx.message.account_keys,
        recent_blockhash=blockhash,
        instructions=tx.message.instructions,
        address_table_lookups=tx.message.address_table_lookups,  # type: ignore
    )

    new_tx = VersionedTransaction(
        message=message,
        keypairs=[keypair],
    )

    signature = keypair.sign_message(to_bytes_versioned(message))

    new_tx.signatures = [signature]

    # ---------- enviar ----------
    print("→ Enviando via Helius RPC...")
    simulation = client.simulate_transaction(new_tx)
    if simulation.value.err:
        raise Exception(f"Erro ao simular transação: {simulation.value}")
    resp = client.send_raw_transaction(bytes(new_tx))
    signature = resp.value

    print(f"✓ Transação enviada: {signature}")
    print("→ Aguardando confirmação...")

    wait_for_confirmation(signature)

    print("✓ Transação confirmada!")
    return {"signature": signature}


def wait_for_confirmation(signature, timeout=30):
    start = time.time()

    while True:
        result = client.get_signature_statuses([signature])
        status = result.value[0]

        if status is not None:
            # Se a transação foi processada
            if status.confirmation_status in [
                TransactionConfirmationStatus.Confirmed,
                TransactionConfirmationStatus.Finalized,
            ]:
                return True
            if status.err is not None:
                raise Exception(f"Transação falhou: {status.err}")

        # Timeout
        if time.time() - start > timeout:
            raise TimeoutError("Transação não foi confirmada a tempo.")


def get_account_balance() -> list[AccountBalanceData]:
    """
    Obtém saldo da wallet Solana.

    NOTA: Requer implementação de RPC calls para Solana.
    Por enquanto, retorna lista vazia.

    Args:
        account_id: ID da conta (ignorado, usa wallet_public_key)

    Returns:
        List[AccountBalanceData]: Lista de saldos
    """
    balances = []

    # ============================
    # 1 - Saldo de SOL (lamports)
    # ============================
    resp = client.get_account_info(wallet)
    if resp.value:
        lamports = resp.value.lamports
        sol = lamports / 1_000_000

        if sol > 0:
            balances.append(
                AccountBalanceData(
                    available=Decimal(str(sol)),
                    on_hold=Decimal("0"),
                    symbol="SOL",
                    total=Decimal(str(sol)),
                )
            )

    # ====================================================
    # 2 - Listar todas as contas SPL pertencentes à wallet
    # ====================================================
    for token in [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]:
        token_accounts = client.get_token_accounts_by_owner(
            wallet, TokenAccountOpts(program_id=token)
        )

        for token_acc in token_accounts.value:
            info = token_acc.account.data  # base64 data
            decoded = bytes(info)

            # Estrutura da SPL Token Account (primeiros 8 bytes = quantidade)
            amount = int.from_bytes(decoded[64:72], "little")

            # Mint (posição fixa)
            mint = Pubkey(decoded[0:32])
            symbol = SOLANA_TOKENS_BY_MINT.get(str(mint))
            if not symbol:
                continue

            # Para converter o token corretamente, buscamos dados do mint
            mint_info = client.get_account_info(mint)

            if not mint_info.value:
                continue

            mint_raw = bytes(mint_info.value.data)
            decimals = mint_raw[44]
            real_amount = amount / (10**decimals)
            if real_amount > 0:
                balances.append(
                    AccountBalanceData(
                        available=Decimal.from_float(real_amount),
                        on_hold=Decimal("0"),
                        symbol=symbol,
                        total=Decimal.from_float(real_amount),
                    )
                )

    return balances


if __name__ == "__main__":
    balances = get_account_balance()
    quantity = next(
        (
            balance.available * Decimal("0.997")
            for balance in balances
            if balance.symbol == "JUP"
        ),
        Decimal("0"),
    )
    do_swap("JUP", "USDC", float(quantity), slippage_bps=50)
