.PHONY: test test-verbose test-coverage install install-dev clean build help run

# Variáveis
PYTHON := python
UV := uv
PYTEST := pytest

help: ## Mostra esta mensagem de ajuda
	@echo "Comandos disponíveis:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Instala as dependências do projeto
	$(UV) sync

install-dev: ## Instala as dependências de desenvolvimento
	$(UV) sync --extra dev

test: ## Executa os testes
	$(UV) run $(PYTEST) .

build: ## Constrói o pacote
	$(UV) build

clean: ## Remove arquivos temporários e cache
	rm -rf dist/
	rm -rf .pytest_cache/
	rm -rf __pycache__/
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} +

runfake: ## Executa o bot principal com API fake e estratégia burra
	$(UV) run $(PYTHON) main.py fake BTC-BRL iteration 1 'sell_on_iteration=2 buy_on_iteration=1'

backtest: ## Executa o bot principal com API fake e estratégia burra
	$(UV) run $(PYTHON) main.py backtest BTC-BRL iteration 60 2025-06-01 2025-06-15 'sell_on_iteration=10 buy_on_iteration=10'

run: ## Executa o bot principal
	$(UV) run $(PYTHON) main.py run $(ARGS)

lint: ## Executa verificação de código com Ruff
	$(UV) run ruff check .

lint-fix: ## Executa verificação e corrige automaticamente problemas com Ruff
	$(UV) run ruff check --fix .

typing-check: ## Executa verificação de typing com pyright
	$(UV) run pyright .


format: ## Formata o código com Ruff
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	$(UV) run pyright ./trader/*

setup-pre-commit: ## Configura hook de pré-commit para verificação automática
	cp scripts/pre-commit.sh .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "✅ Hook de pré-commit configurado! O código será verificado automaticamente antes de cada commit."
