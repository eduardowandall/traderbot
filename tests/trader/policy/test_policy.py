from decimal import Decimal

import pytest

from trader.models import SOLANA_MINTS
from trader.models.intent import IntentSide, TradeIntent
from trader.policy import Policy, PolicyState, evaluate, load_policy

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint


def _intent(
    side=IntentSide.BUY, notional: str | None = "10", spend_amount="10", **kwargs
):
    return TradeIntent(
        source="test",
        account="dry:SOL-USDC",
        side=side,
        spend_mint=kwargs.pop("spend_mint", USDC),
        receive_mint=kwargs.pop("receive_mint", SOL),
        spend_amount=Decimal(spend_amount),
        notional_usd=None if notional is None else Decimal(notional),
        **kwargs,
    )


def _eval(intent=None, policy=None, state=None, halted=False, real_mode=False):
    return evaluate(
        intent or _intent(),
        policy or Policy(),
        state or PolicyState(),
        halted=halted,
        real_mode=real_mode,
    )


class TestEvaluate:
    def test_allows_a_small_dry_trade(self):
        decision = _eval()
        assert decision.allowed
        assert decision.reasons == ()
        assert decision.policy_version == "defaults"

    def test_kill_switch(self):
        assert "kill switch" in _eval(halted=True).reasons[0]

    def test_real_mode_requires_opt_in(self):
        assert not _eval(real_mode=True).allowed
        assert _eval(real_mode=True, policy=Policy(real_trading_enabled=True)).allowed

    def test_unresolved_intents_block_everything(self):
        state = PolicyState(unresolved_intent_ids=("abc",))
        decision = _eval(state=state)
        assert not decision.allowed
        assert "abc" in decision.reasons[0]
        assert not _eval(_intent(side=IntentSide.SELL), state=state).allowed

    def test_circuit_breaker(self):
        assert not _eval(state=PolicyState(consecutive_failures=3)).allowed
        assert _eval(state=PolicyState(consecutive_failures=2)).allowed

    def test_unknown_mint(self):
        decision = _eval(_intent(receive_mint="NotAMint"))
        assert "mint desconhecido: NotAMint" in decision.reasons

    def test_allow_list(self):
        policy = Policy(allowed_symbols=("USDC", "SOL"))
        assert _eval(policy=policy).allowed
        jup = SOLANA_MINTS.get_by_symbol("JUP").mint
        decision = _eval(_intent(receive_mint=jup), policy=policy)
        assert "símbolo não permitido: JUP" in decision.reasons

    def test_spend_amount_must_be_positive(self):
        assert not _eval(_intent(spend_amount="0")).allowed

    def test_unknown_notional(self):
        assert not _eval(_intent(notional=None)).allowed
        assert _eval(
            _intent(notional=None), policy=Policy(allow_unknown_notional=True)
        ).allowed

    def test_max_trade(self):
        assert _eval(_intent(notional="25")).allowed
        assert not _eval(_intent(notional="25.01")).allowed

    def test_daily_notional(self):
        state = PolicyState(daily_notional_usd=Decimal("95"))
        assert _eval(_intent(notional="5"), state=state).allowed
        assert not _eval(_intent(notional="5.01"), state=state).allowed

    def test_trades_per_hour(self):
        assert not _eval(state=PolicyState(trades_last_hour=10)).allowed

    def test_daily_loss_blocks_buys(self):
        state = PolicyState(daily_realized_pnl_usd=Decimal("-20"))
        assert not _eval(state=state).allowed
        assert _eval(
            state=PolicyState(daily_realized_pnl_usd=Decimal("-19.99"))
        ).allowed

    def test_sells_bypass_budget_limits(self):
        # sair da posição nunca é bloqueado por orçamento
        state = PolicyState(
            daily_notional_usd=Decimal("1000"),
            trades_last_hour=99,
            daily_realized_pnl_usd=Decimal("-500"),
        )
        sell = _intent(side=IntentSide.SELL, notional="1000")
        assert _eval(sell, state=state).allowed

    def test_swaps_are_budgeted(self):
        assert not _eval(_intent(side=IntentSide.SWAP, notional="100")).allowed

    def test_collects_every_reason(self):
        decision = _eval(_intent(notional="1000"), halted=True, real_mode=True)
        assert len(decision.reasons) == 4


class TestLoadPolicy:
    def test_defaults_without_file(self, tmp_path):
        assert load_policy(tmp_path / "missing.toml") == Policy()

    def test_reads_file_and_versions_it(self, tmp_path):
        path = tmp_path / "policy.toml"
        path.write_text(
            "[trading]\nreal_trading_enabled = true\n"
            'allowed_symbols = ["SOL", "USDC"]\n'
            "[limits]\nmax_trade_usd = 0.1\nmax_trades_per_hour = 2\n",
            encoding="utf-8",
        )
        policy = load_policy(path)
        assert policy.real_trading_enabled is True
        assert policy.allowed_symbols == ("SOL", "USDC")
        assert policy.max_trade_usd == Decimal("0.1")
        assert policy.max_trades_per_hour == 2
        assert len(policy.version) == 12

    def test_env_var_path(self, tmp_path, monkeypatch):
        path = tmp_path / "custom.toml"
        path.write_text("[limits]\nmax_daily_loss_usd = 5\n", encoding="utf-8")
        monkeypatch.setenv("TRADER_POLICY_FILE", str(path))
        assert load_policy().max_daily_loss_usd == Decimal("5")

    @pytest.mark.parametrize(
        "content",
        [
            "[limits]\nmax_trade_usd = -1\n",
            "[limits]\nmax_trades_per_hour = 1.5\n",
            "[limits]\nmax_trades_per_hour = true\n",
            "[trading]\nreal_trading_enabled = 1\n",
            '[trading]\nallowed_symbols = ["NOPE"]\n',
            "[limits]\ntypo_limit = 1\n",
        ],
    )
    def test_rejects_invalid_files(self, tmp_path, content):
        path = tmp_path / "policy.toml"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_policy(path)


def test_every_policy_field_has_a_parser():
    from dataclasses import fields

    from trader.policy.policy import _PARSERS

    assert set(_PARSERS) == {f.name for f in fields(Policy)} - {"version"}


MODE_POLICY = """
[limits]
max_trade_usd = 25
max_daily_notional_usd = 100

[paper.limits]
max_daily_notional_usd = 1000
max_trades_per_hour = 60

[real.trading]
real_trading_enabled = true
"""


class TestPerModePolicy:
    def _load(self, tmp_path, mode, content=MODE_POLICY):
        path = tmp_path / "policy.toml"
        path.write_text(content, encoding="utf-8")
        return load_policy(path, mode=mode)

    def test_mode_section_overrides_only_its_keys(self, tmp_path):
        paper = self._load(tmp_path, "paper")
        assert paper.max_daily_notional_usd == Decimal("1000")
        assert paper.max_trades_per_hour == 60
        assert paper.max_trade_usd == Decimal("25")  # herdado da base
        assert paper.real_trading_enabled is False
        assert paper.version.endswith("/paper")

    def test_other_modes_keep_base_limits(self, tmp_path):
        dry = self._load(tmp_path, "dry")
        assert dry.max_daily_notional_usd == Decimal("100")
        assert dry.max_trades_per_hour == 10
        assert "/" not in dry.version

        real = self._load(tmp_path, "real")
        assert real.real_trading_enabled is True
        assert real.max_daily_notional_usd == Decimal("100")

    def test_without_mode_ignores_overrides(self, tmp_path):
        assert self._load(tmp_path, None).max_daily_notional_usd == Decimal("100")

    @pytest.mark.parametrize(
        "content",
        [
            # erro na seção de outro modo também é recusado
            "[real.limits]\nmax_trade_usd = -1\n",
            "[real.limits]\ntypo = 1\n",
            "[paper]\nmax_trade_usd = 1\n",
            "[paper.extras]\nx = 1\n",
            "[staging.limits]\nmax_trade_usd = 1\n",
            "[limit]\nmax_trade_usd = 1\n",  # typo de [limits]
            "limits = 5\n",
        ],
    )
    def test_rejects_invalid_mode_sections(self, tmp_path, content):
        with pytest.raises(ValueError):
            self._load(tmp_path, "paper", content)

    def test_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(ValueError, match="modo"):
            self._load(tmp_path, "staging")
