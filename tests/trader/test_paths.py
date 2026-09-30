from trader.ledger import ledger_path
from trader.paths import PROJECT_ROOT, data_dir, policy_file
from trader.policy import load_policy


def test_defaults_are_under_project_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADER_DATA_DIR")
    monkeypatch.delenv("TRADER_POLICY_FILE")
    monkeypatch.chdir(tmp_path)

    # só compara caminhos: nada é escrito nos arquivos reais do projeto
    assert data_dir() == PROJECT_ROOT / ".data"
    assert policy_file() == PROJECT_ROOT / "policy.toml"
    assert ledger_path("real") == PROJECT_ROOT / ".data" / "ledger-real.sqlite3"
    assert (PROJECT_ROOT / "pyproject.toml").exists()


def test_absolute_env_values_are_used_as_is(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRADER_POLICY_FILE", str(tmp_path / "p.toml"))

    assert data_dir() == tmp_path / "state"
    assert policy_file() == tmp_path / "p.toml"


def test_relative_env_values_resolve_against_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_DATA_DIR", "state")
    monkeypatch.setenv("TRADER_POLICY_FILE", "conf/p.toml")
    monkeypatch.chdir(tmp_path)

    assert data_dir() == PROJECT_ROOT / "state"
    assert policy_file() == PROJECT_ROOT / "conf" / "p.toml"


def test_policy_is_found_from_another_directory(tmp_path, monkeypatch):
    policy_file().write_text("[limits]\nmax_trade_usd = 7\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert load_policy().max_trade_usd == 7
