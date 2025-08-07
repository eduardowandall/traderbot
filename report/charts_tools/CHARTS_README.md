# Geração de Gráficos dos Dados de Trading

Este documento explica como gerar gráficos a partir dos dados de trading salvos pelo bot.

## Estrutura de Pastas

O bot agora organiza os arquivos da seguinte forma:
- `data/`: Contém os arquivos CSV com dados de trading
- `charts/`: Contém os gráficos gerados (criada automaticamente)

Ambas as pastas estão no `.gitignore` para não versionar dados pessoais de trading.

## Instalação das Dependências

Para gerar gráficos, você precisa instalar as dependências adicionais:

```bash
make install-charts
```

Ou usando uv diretamente:
```bash
uv sync --extra charts
```

## Uso do Script de Gráficos

O script `generate_charts.py` permite gerar vários tipos de gráficos a partir dos dados CSV:

### Sintaxe Básica

```bash
make charts ARGS="<arquivo_csv> [opções]"
```

Ou diretamente:
```bash
python charts_tools/generate_charts.py <arquivo_csv> [opções]
```

### Exemplos

1. **Gerar gráficos e salvar em arquivos PNG (arquivo na pasta data/):**
```bash
make charts ARGS="trading_data_BTC_BRL.csv"
```

2. **Gerar gráficos especificando caminho completo:**
```bash
make charts ARGS="data/trading_data_BTC_BRL.csv"
```

3. **Mostrar gráficos na tela:**
```bash
make charts ARGS="trading_data_BTC_BRL.csv --show"
```

4. **Salvar em diretório específico:**
```bash
make charts ARGS="trading_data_BTC_BRL.csv --output-dir meus_graficos"
```

5. **Usando Python diretamente:**
```bash
python charts_tools/generate_charts.py trading_data_BTC_BRL.csv
```

## Tipos de Gráficos Gerados

### 1. Gráfico de Preços
- Mostra a evolução do preço ao longo do tempo
- Marca os pontos de compra (triângulos verdes) e venda (triângulos vermelhos)
- Inclui linhas verticais tracejadas nos momentos das operações
- Anotações com horário exato das operações de compra e venda
- Arquivo: `{nome_do_csv}_price.png`

### 2. Gráfico de PnL (Profit and Loss)
- **PnL Não Realizado**: Lucro/prejuízo da posição atual
- **PnL Realizado**: Lucro/prejuízo acumulado de posições fechadas
- Arquivo: `{nome_do_csv}_pnl.png`

### 3. Gráfico de Posições
- Mostra quando o bot estava em posição long, short ou sem posição
- Arquivo: `{nome_do_csv}_positions.png`

## Formato dos Dados CSV

O arquivo CSV gerado pelo bot contém as seguintes colunas:

| Coluna | Descrição |
|--------|-----------|
| `timestamp` | Data e hora da iteração |
| `symbol` | Par de moedas (ex: BTC-BRL) |
| `price` | Preço atual (arredondado para 2 casas decimais) |
| `position_type` | Lado da posição (long/short ou vazio) |
| `position_quantity` | Quantidade da posição |
| `position_entry_price` | Preço de entrada da posição (arredondado para 2 casas decimais) |
| `unrealized_pnl` | PnL não realizado (arredondado para 2 casas decimais) |
| `realized_pnl` | PnL realizado acumulado (arredondado para 2 casas decimais) |
| `signal` | Sinal de trading (buy/sell ou vazio) |

## Estatísticas Resumidas

O script também gera estatísticas resumidas incluindo:
- Período dos dados
- Total de registros
- Preços mínimo, máximo e médio
- PnL final realizado e não realizado
- Contagem de sinais de compra e venda

## Opções do Script

- `csv_file`: Arquivo CSV com dados de trading. Se apenas o nome for fornecido, o script procurará automaticamente na pasta `data/`
- `--output-dir, -o`: Diretório para salvar os gráficos (padrão: `charts`)
- `--show, -s`: Mostrar gráficos na tela em vez de salvar em arquivos
- `--help, -h`: Mostrar ajuda

## Exemplo de Saída

```
📊 ESTATÍSTICAS RESUMIDAS
==================================================
Período: 2025-07-29 22:59:37.129485 até 2025-07-29 23:05:42.192204
Total de registros: 25
Preço mínimo: R$ 658740.00
Preço máximo: R$ 658740.00
Preço médio: R$ 658740.00

PnL Final Realizado: R$ 0.00
PnL Final Não Realizado: R$ 0.00
PnL Total: R$ 0.00

Sinais de Compra: 1
Sinais de Venda: 0

✅ Todos os gráficos foram salvos no diretório: charts
```

## Personalização

Você pode modificar o script `generate_charts.py` para:
- Adicionar novos tipos de gráficos
- Alterar cores e estilos
- Incluir indicadores técnicos
- Modificar o formato de saída
