"""
Script para gerar gráficos a partir dos dados de trading salvos em CSV.
"""

import argparse
import os
from typing import cast

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


def load_trading_data(csv_file: str) -> pd.DataFrame:
    """Carrega os dados de trading do arquivo CSV"""
    if not os.path.exists(csv_file):
        raise FileNotFoundError(f"Arquivo {csv_file} não encontrado")

    df = pd.read_csv(csv_file)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def plot_price_chart(df: pd.DataFrame, symbol: str, output_file: str | None = None):
    """Gera gráfico de preços ao longo do tempo"""
    plt.figure(figsize=(12, 6))
    plt.plot(df["timestamp"], df["price"], linewidth=2, color="blue", label="Preço")

    # Marcar sinais de compra e venda
    buy_signals = df[df["signal"] == "buy"]
    sell_signals = df[df["signal"] == "sell"]

    if not buy_signals.empty:
        plt.scatter(
            buy_signals["timestamp"],
            buy_signals["price"],
            color="green",
            marker="^",
            s=100,
            label="Compra",
            zorder=5,
        )

        # Adicionar linhas verticais e anotações para operações de compra
        for _, signal in buy_signals.iterrows():
            # Garantir que estamos extraindo valores escalares
            timestamp_raw = signal.loc["timestamp"]
            price_raw = signal.loc["price"]
            timestamp_val = pd.Timestamp(timestamp_raw)
            price_val = float(price_raw)
            timestamp_num = float(mdates.date2num(timestamp_val))
            plt.axvline(
                x=timestamp_num,
                color="green",
                linestyle="--",
                alpha=0.7,
                linewidth=1,
            )
            # Adicionar anotação com o horário da operação
            if hasattr(timestamp_val, "strftime"):
                time_str = cast(pd.Timestamp, timestamp_val).strftime("%H:%M:%S")
            else:
                time_str = "N/A"
            plt.annotate(
                f"COMPRA\n{time_str}",
                xy=(timestamp_num, price_val),
                xytext=(10, 20),
                textcoords="offset points",
                bbox={
                    "boxstyle": "round,pad=0.3",
                    "facecolor": "lightgreen",
                    "alpha": 0.7,
                },
                arrowprops={"arrowstyle": "->", "connectionstyle": "arc3,rad=0"},
                fontsize=8,
                ha="left",
            )

    if not sell_signals.empty:
        plt.scatter(
            sell_signals["timestamp"],
            sell_signals["price"],
            color="red",
            marker="v",
            s=100,
            label="Venda",
            zorder=5,
        )

        # Adicionar linhas verticais e anotações para operações de venda
        for _, signal in sell_signals.iterrows():
            # Garantir que estamos extraindo valores escalares
            timestamp_raw = signal.loc["timestamp"]
            price_raw = signal.loc["price"]
            timestamp_val = pd.Timestamp(timestamp_raw)
            price_val = float(price_raw)
            timestamp_num = float(mdates.date2num(timestamp_val))
            plt.axvline(
                x=timestamp_num,
                color="red",
                linestyle="--",
                alpha=0.7,
                linewidth=1,
            )
            # Adicionar anotação com o horário da operação
            if hasattr(timestamp_val, "strftime"):
                time_str = cast(pd.Timestamp, timestamp_val).strftime("%H:%M:%S")
            else:
                time_str = "N/A"
            plt.annotate(
                f"VENDA\n{time_str}",
                xy=(timestamp_num, price_val),
                xytext=(10, -30),
                textcoords="offset points",
                bbox={
                    "boxstyle": "round,pad=0.3",
                    "facecolor": "lightcoral",
                    "alpha": 0.7,
                },
                arrowprops={"arrowstyle": "->", "connectionstyle": "arc3,rad=0"},
                fontsize=8,
                ha="left",
            )

    plt.title(f"Preço do {symbol} ao Longo do Tempo")
    plt.xlabel("Tempo")
    plt.ylabel("Preço (R$)")
    plt.legend()
    plt.grid(True, alpha=0.3)

    # Configurar eixo x com marcações especiais para operações
    ax = plt.gca()

    # Coletar todos os timestamps de operações
    operation_times = []
    if not buy_signals.empty:
        operation_times.extend(buy_signals["timestamp"].tolist())
    if not sell_signals.empty:
        operation_times.extend(sell_signals["timestamp"].tolist())

    # Formatar eixo x para datas
    ax.xaxis.set_major_formatter(mdates.DateFormatter(""))  # Remove os rótulos
    ax.set_xticklabels([])  # Remove os textos do eixo X

    # Adicionar marcações menores para operações se houver
    if operation_times:
        # Adicionar localizador menor para mostrar operações
        ax.xaxis.set_minor_locator(mdates.AutoDateLocator(maxticks=10))
        ax.xaxis.set_minor_formatter(mdates.DateFormatter(""))

        # Destacar os timestamps das operações
        for op_time in operation_times:
            # Adicionar tick personalizado para a operação
            ax.axvline(
                x=op_time, color="black", linestyle=":", alpha=0.5, linewidth=0.5
            )

    plt.xticks(rotation=45)
    plt.tight_layout()

    if output_file:
        plt.savefig(output_file, dpi=300, bbox_inches="tight")
        print(f"Gráfico de preços salvo em: {output_file}")
    else:
        plt.show()


def plot_pnl_chart(df: pd.DataFrame, output_file: str | None = None):
    """Gera gráfico de PnL (lucro/prejuízo) ao longo do tempo"""
    plt.figure(figsize=(12, 6))

    # PnL não realizado
    plt.subplot(2, 1, 1)
    plt.plot(
        df["timestamp"],
        df["unrealized_pnl"],
        linewidth=2,
        color="orange",
        label="PnL Não Realizado",
    )
    plt.axhline(y=0, color="black", linestyle="--", alpha=0.5)
    plt.title("PnL Não Realizado ao Longo do Tempo")
    plt.ylabel("PnL (R$)")
    plt.legend()
    plt.grid(True, alpha=0.3)

    # PnL realizado
    plt.subplot(2, 1, 2)
    plt.plot(
        df["timestamp"],
        df["realized_pnl"],
        linewidth=2,
        color="purple",
        label="PnL Realizado",
    )
    plt.axhline(y=0, color="black", linestyle="--", alpha=0.5)
    plt.title("PnL Realizado Acumulado ao Longo do Tempo")
    plt.xlabel("Tempo")
    plt.ylabel("PnL (R$)")
    plt.legend()
    plt.grid(True, alpha=0.3)

    # Formatar eixo x para datas
    for ax in plt.gcf().get_axes():
        ax.xaxis.set_major_formatter(mdates.DateFormatter(""))  # Remove os rótulos
        ax.set_xticklabels([])  # Remove os textos do eixo X
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)

    plt.tight_layout()

    if output_file:
        plt.savefig(output_file, dpi=300, bbox_inches="tight")
        print(f"Gráfico de PnL salvo em: {output_file}")
    else:
        plt.show()


def plot_position_chart(df: pd.DataFrame, output_file: str | None = None):
    """Gera gráfico mostrando as posições ao longo do tempo"""
    plt.figure(figsize=(12, 4))

    # Criar dados para o gráfico de posições
    position_data = []
    for _, row in df.iterrows():
        if row["position_type"] == "long":
            position_data.append(1)
        elif row["position_type"] == "short":
            position_data.append(-1)
        else:
            position_data.append(0)

    plt.plot(df["timestamp"], position_data, linewidth=3, drawstyle="steps-post")
    plt.fill_between(df["timestamp"], position_data, alpha=0.3)

    plt.title("Posições ao Longo do Tempo")
    plt.xlabel("Tempo")
    plt.ylabel("Posição")
    plt.yticks([-1, 0, 1], ["Short", "Sem Posição", "Long"])
    plt.grid(True, alpha=0.3)

    # Formatar eixo x para datas
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%"))
    # plt.gca().xaxis.set_major_locator(mdates.MinuteLocator(interval=5))
    plt.xticks(rotation=45)

    plt.tight_layout()

    if output_file:
        plt.savefig(output_file, dpi=300, bbox_inches="tight")
        print(f"Gráfico de posições salvo em: {output_file}")
    else:
        plt.show()


def generate_summary_stats(df: pd.DataFrame):
    """Gera estatísticas resumidas dos dados de trading"""
    print("\n📊 ESTATÍSTICAS RESUMIDAS")
    print("=" * 50)

    # Estatísticas básicas
    print(f"Período: {df['timestamp'].min()} até {df['timestamp'].max()}")
    print(f"Total de registros: {len(df)}")
    print(f"Preço mínimo: R$ {df['price'].min():.2f}")
    print(f"Preço máximo: R$ {df['price'].max():.2f}")
    print(f"Preço médio: R$ {df['price'].mean():.2f}")

    # Estatísticas de PnL
    final_realized_pnl = df["realized_pnl"].iloc[-1]
    final_unrealized_pnl = df["unrealized_pnl"].iloc[-1]
    total_pnl = final_realized_pnl + final_unrealized_pnl

    print(f"\nPnL Final Realizado: R$ {final_realized_pnl:.2f}")
    print(f"PnL Final Não Realizado: R$ {final_unrealized_pnl:.2f}")
    print(f"PnL Total: R$ {total_pnl:.2f}")

    # Contagem de sinais
    buy_signals = len(df[df["signal"] == "buy"])
    sell_signals = len(df[df["signal"] == "sell"])

    print(f"\nSinais de Compra: {buy_signals}")
    print(f"Sinais de Venda: {sell_signals}")


def main():
    parser = argparse.ArgumentParser(description="Gera gráficos dos dados de trading")
    parser.add_argument(
        "csv_file",
        help="Arquivo CSV com os dados de trading (pode ser apenas o nome se estiver na pasta data/)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="report/charts",
        help="Diretório para salvar os gráficos (default: report/charts)",
    )
    parser.add_argument(
        "--show",
        "-s",
        action="store_true",
        help="Mostrar gráficos na tela em vez de salvar",
    )

    args = parser.parse_args()

    # Se o arquivo não existe no caminho atual, tentar na pasta data/
    csv_file = args.csv_file
    if not os.path.exists(csv_file) and not csv_file.startswith("report/data/"):
        data_file = os.path.join("data", csv_file)
        if os.path.exists(data_file):
            csv_file = data_file

    try:
        # Carregar dados
        df = load_trading_data(csv_file)
        symbol = df["symbol"].iloc[0] if not df.empty else "Unknown"

        # Criar diretório de saída se não existir
        if not args.show:
            os.makedirs(args.output_dir, exist_ok=True)

        # Gerar estatísticas
        generate_summary_stats(df)

        # Gerar gráficos
        if args.show:
            plot_price_chart(df, symbol)
            plot_pnl_chart(df)
            plot_position_chart(df)
        else:
            base_name = os.path.splitext(os.path.basename(csv_file))[0]
            plot_price_chart(df, symbol, f"{args.output_dir}/{base_name}_price.png")
            plot_pnl_chart(df, f"{args.output_dir}/{base_name}_pnl.png")
            plot_position_chart(df, f"{args.output_dir}/{base_name}_positions.png")

            print(
                f"\n✅ Todos os gráficos foram salvos no diretório: {args.output_dir}"
            )

    except Exception as e:
        print(f"❌ Erro: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
