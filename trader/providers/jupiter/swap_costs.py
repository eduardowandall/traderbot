"""Custos reais de um swap, lidos da transação confirmada (`getTransaction`).

Regras (ver docs/plan.md §7.1, "Costs and net PnL"):
- índice 0 das contas estáticas é o fee payer; ele precisa ser a carteira;
- `meta.fee` = taxa base (5000/assinatura) + priority fee;
- rent = variação de lamports das contas de token (não-wSOL) da carteira:
  positiva ao abrir uma conta, negativa ao fechar (reembolso);
- as pernas em token vêm da variação dos saldos de token da carteira;
- a perna em SOL (a Jupiter embrulha/desembrulha SOL) sai de
  S = Δnativo + ΔwSOL + rent + fee, que vale perna_SOL − outros_débitos.
  Em ExactIn a entrada é exatamente `quote.inAmount`: com SOL na entrada
  resolve-se "outros"; com SOL na saída assume-se outros = 0.
"""

from dataclasses import dataclass

from solders.pubkey import Pubkey

from trader.models.costs import BASE_FEE_LAMPORTS, ONCHAIN, TradeCosts
from trader.models.mints import SOLANA_MINTS

WSOL_MINT = SOLANA_MINTS.get_by_symbol("SOL").mint


@dataclass(frozen=True)
class SwapLegs:
    """Mints e valores da quote do swap sendo analisado."""

    input_mint: str
    output_mint: str
    quoted_in: int
    quoted_out: int


@dataclass
class _Deltas:
    tokens: dict[str, int]  # variação raw por mint (contas da carteira)
    wsol_lamports: int
    rent_lamports: int


def _owned_accounts(meta, wallet: Pubkey) -> dict[int, str]:
    """{índice da conta: mint} das contas de token da carteira (pré ∪ pós)."""
    balances = (meta.pre_token_balances or []) + (meta.post_token_balances or [])
    return {b.account_index: str(b.mint) for b in balances if b.owner == wallet}


def _token_amounts(balances, wallet: Pubkey) -> dict[int, int]:
    return {
        b.account_index: int(b.ui_token_amount.amount)
        for b in balances or []
        if b.owner == wallet
    }


def _deltas(meta, wallet: Pubkey) -> _Deltas:
    accounts = _owned_accounts(meta, wallet)
    pre = _token_amounts(meta.pre_token_balances, wallet)
    post = _token_amounts(meta.post_token_balances, wallet)
    tokens: dict[str, int] = {}
    wsol = rent = 0
    for index, mint in accounts.items():
        tokens[mint] = tokens.get(mint, 0) + post.get(index, 0) - pre.get(index, 0)
        lamports = meta.post_balances[index] - meta.pre_balances[index]
        if mint == WSOL_MINT:
            wsol += lamports
        else:
            rent += lamports
    return _Deltas(tokens, wsol, rent)


def _legs(legs: SwapLegs, deltas: _Deltas, sol_flow: int) -> tuple[int, int, int]:
    """(entrada efetiva, saída efetiva, outros débitos) em raw/lamports."""
    if legs.input_mint == WSOL_MINT:
        # sol_flow = -entrada - outros
        return (
            legs.quoted_in,
            deltas.tokens.get(legs.output_mint, 0),
            max(-sol_flow - legs.quoted_in, 0),
        )
    if legs.output_mint == WSOL_MINT:
        # sol_flow = saída - outros; sem como separar, assume outros = 0
        return -deltas.tokens.get(legs.input_mint, 0), sol_flow, 0
    return (
        -deltas.tokens.get(legs.input_mint, 0),
        deltas.tokens.get(legs.output_mint, 0),
        max(-sol_flow, 0),
    )


def parse_swap_costs(
    meta,
    account_keys: list[Pubkey],
    signature_count: int,
    wallet: Pubkey,
    legs: SwapLegs,
    source: str = ONCHAIN,
) -> TradeCosts | None:
    """None quando a transação não permite uma leitura confiável."""
    if meta is None or meta.err is not None:
        return None
    if not account_keys or account_keys[0] != wallet:
        return None  # a carteira não pagou a transação: leitura não confiável
    deltas = _deltas(meta, wallet)
    native = meta.post_balances[0] - meta.pre_balances[0]
    sol_flow = native + deltas.wsol_lamports + deltas.rent_lamports + meta.fee
    actual_in, actual_out, other = _legs(legs, deltas, sol_flow)
    return TradeCosts(
        source=source,
        fee_lamports=meta.fee,
        priority_fee_lamports=max(meta.fee - BASE_FEE_LAMPORTS * signature_count, 0),
        rent_lamports=deltas.rent_lamports,
        other_lamports=other,
        actual_in_amount=actual_in,
        actual_out_amount=actual_out,
        quoted_out_amount=legs.quoted_out,
    )


def quote_info(quote) -> dict:
    """Campos informativos da quote: LP fees por mint e impacto de preço."""
    if quote is None:
        return {}
    lp_fees: dict[str, int] = {}
    for step in quote.routePlan:
        info = step.swapInfo
        if info.feeAmount and info.feeMint:
            lp_fees[info.feeMint] = lp_fees.get(info.feeMint, 0) + int(info.feeAmount)
    return {"lp_fees": lp_fees, "price_impact_pct": quote.priceImpactPct}
