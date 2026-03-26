"""
Weekly PDF report generator.

Creates a 3-page PDF with:
  1. Balance evolution over time (line chart)
  2. Win rate by market (grouped bar chart)
  3. P&L per trade + cumulative P&L (bar + line overlay)

Then sends the PDF to Telegram via sendDocument.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import requests

from logger import get_logger

log = get_logger()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_trades(trades_file: str = "paper_trades.json") -> dict:
    try:
        with open(trades_file, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"trades": [], "current_balance": 0, "realized_pnl": 0}


def _parse_ts(ts_str: str) -> float:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_weekly_pdf(
    starting_balance: float,
    trades_file: str = "paper_trades.json",
    output_path: str = "weekly_report.pdf",
) -> str | None:
    """
    Generates the PDF report. Returns the file path or None on failure.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")   # headless — no display required
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.backends.backend_pdf import PdfPages
    except ImportError:
        log.error("[PDF] matplotlib no instalado. Ejecuta: pip install matplotlib")
        return None

    data          = _load_trades(trades_file)
    trades        = data.get("trades", [])
    cur_balance   = data.get("current_balance", starting_balance)
    realized_pnl  = data.get("realized_pnl", 0.0)

    # Only closed trades (SELL with pnl)
    sells = sorted(
        [t for t in trades if t["action"] == "SELL" and t.get("pnl") is not None],
        key=lambda x: _parse_ts(x["timestamp"]),
    )

    # ---- Balance timeline ----
    bal_dates: list[datetime] = [datetime.now()]
    bal_vals:  list[float]    = [starting_balance]
    running = starting_balance
    for t in sells:
        running += t["pnl"]
        bal_dates.append(datetime.fromtimestamp(_parse_ts(t["timestamp"])))
        bal_vals.append(running)

    # ---- P&L per trade ----
    pnl_values = [t["pnl"] for t in sells]
    pnl_colors = ["#27ae60" if p >= 0 else "#e74c3c" for p in pnl_values]

    # ---- Win/loss by market ----
    wins:   dict[str, int] = {}
    losses: dict[str, int] = {}
    for t in sells:
        lbl = t.get("label", "Unknown")[:22]
        if t["pnl"] > 0:
            wins[lbl]   = wins.get(lbl, 0) + 1
        else:
            losses[lbl] = losses.get(lbl, 0) + 1
    all_markets = sorted(set(list(wins) + list(losses)))

    total_w  = sum(wins.values())
    total_l  = sum(losses.values())
    total_tr = total_w + total_l
    win_rate = total_w / total_tr * 100 if total_tr else 0.0
    ret      = cur_balance - starting_balance
    ret_pct  = ret / starting_balance * 100 if starting_balance else 0.0
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    week_str = datetime.now().strftime("Semana del %d/%m/%Y")

    _STYLE = {
        "axes.facecolor":  "#1a1a2e",
        "figure.facecolor": "#16213e",
        "axes.edgecolor":  "#4a4a6a",
        "axes.labelcolor": "#e0e0e0",
        "xtick.color":     "#e0e0e0",
        "ytick.color":     "#e0e0e0",
        "text.color":      "#e0e0e0",
        "grid.color":      "#2a2a4a",
        "grid.linestyle":  "--",
        "grid.alpha":      0.5,
    }

    with PdfPages(output_path) as pdf:

        # ---- Page 1: Balance evolution ----
        with plt.rc_context(_STYLE):
            fig, ax = plt.subplots(figsize=(11, 5.5))
            if len(bal_dates) > 1:
                ax.plot(bal_dates, bal_vals, color="#3498db", linewidth=2.2,
                        marker="o", markersize=5, zorder=3)
                ax.axhline(starting_balance, color="#95a5a6", linestyle="--",
                           alpha=0.6, label=f"Inicial: {starting_balance:.0f} USDC")
                ax.fill_between(
                    bal_dates, starting_balance, bal_vals,
                    where=[v >= starting_balance for v in bal_vals],
                    alpha=0.18, color="#27ae60",
                )
                ax.fill_between(
                    bal_dates, starting_balance, bal_vals,
                    where=[v < starting_balance for v in bal_vals],
                    alpha=0.18, color="#e74c3c",
                )
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
                plt.xticks(rotation=28, ha="right")
                ax.legend(loc="upper left", framealpha=0.3)
            else:
                ax.text(0.5, 0.5, "Sin trades registrados aún",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=14, color="#7f8c8d")

            sign = "+" if ret >= 0 else ""
            ax.set_title(
                f"Evolución del Balance  —  {week_str}\n"
                f"Retorno: {sign}{ret:.2f} USDC  ({sign}{ret_pct:.1f}%)   "
                f"P&L realizado: {realized_pnl:+.2f} USDC",
                fontsize=12, fontweight="bold", pad=12,
            )
            ax.set_ylabel("Balance (USDC)")
            ax.set_xlabel("Fecha / Hora")
            ax.grid(True)
            fig.tight_layout(pad=1.8)
            pdf.savefig(fig, bbox_inches="tight", facecolor=fig.get_facecolor())
            plt.close(fig)

        # ---- Page 2: Win rate by market ----
        with plt.rc_context(_STYLE):
            fig, ax = plt.subplots(figsize=(11, 5.5))
            if all_markets:
                x     = list(range(len(all_markets)))
                w_vals = [wins.get(m, 0)   for m in all_markets]
                l_vals = [losses.get(m, 0) for m in all_markets]
                width  = 0.36
                b1 = ax.bar([i - width / 2 for i in x], w_vals, width,
                            label="Ganancias", color="#27ae60", alpha=0.88)
                b2 = ax.bar([i + width / 2 for i in x], l_vals, width,
                            label="Pérdidas",  color="#e74c3c", alpha=0.88)
                for bar in list(b1) + list(b2):
                    h = bar.get_height()
                    if h > 0:
                        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.04,
                                str(int(h)), ha="center", va="bottom", fontsize=9,
                                color="#e0e0e0")
                ax.set_xticks(x)
                ax.set_xticklabels(all_markets, rotation=28, ha="right", fontsize=8.5)
                ax.legend(framealpha=0.3)
            else:
                ax.text(0.5, 0.5, "Sin trades registrados aún",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=14, color="#7f8c8d")

            ax.set_title(
                f"Win Rate por Mercado  —  {week_str}\n"
                f"Global: {win_rate:.0f}%  ({total_w}W / {total_l}L  —  {total_tr} trades cerradas)",
                fontsize=12, fontweight="bold", pad=12,
            )
            ax.set_ylabel("Nº de trades")
            ax.grid(True, axis="y")
            fig.tight_layout(pad=1.8)
            pdf.savefig(fig, bbox_inches="tight", facecolor=fig.get_facecolor())
            plt.close(fig)

        # ---- Page 3: P&L per trade + cumulative ----
        with plt.rc_context(_STYLE):
            fig, ax = plt.subplots(figsize=(11, 5.5))
            if pnl_values:
                idx = list(range(len(pnl_values)))
                ax.bar(idx, pnl_values, color=pnl_colors, alpha=0.85, zorder=2)
                ax.axhline(0, color="#95a5a6", linewidth=0.9)

                cum = []
                s = 0.0
                for p in pnl_values:
                    s += p
                    cum.append(s)

                ax2 = ax.twinx()
                ax2.plot(idx, cum, color="#9b59b6", linewidth=2.2,
                         marker=".", markersize=4, label="P&L acumulado", zorder=3)
                ax2.axhline(0, color="#9b59b6", linestyle=":", linewidth=0.8, alpha=0.5)
                ax2.set_ylabel("P&L acumulado (USDC)", color="#9b59b6")
                ax2.tick_params(axis="y", labelcolor="#9b59b6")
                ax2.legend(loc="upper left", framealpha=0.3)

                ax.set_xlabel("Trade #")
                ax.set_ylabel("P&L por trade (USDC)")
            else:
                ax.text(0.5, 0.5, "Sin trades registrados aún",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=14, color="#7f8c8d")

            total_pnl = sum(pnl_values)
            ax.set_title(
                f"P&L por Trade  —  {week_str}\n"
                f"P&L total: {total_pnl:+.2f} USDC   "
                f"Mejor: {max(pnl_values, default=0):+.2f}   "
                f"Peor: {min(pnl_values, default=0):+.2f}",
                fontsize=12, fontweight="bold", pad=12,
            )
            ax.grid(True, axis="y")
            fig.tight_layout(pad=1.8)
            pdf.savefig(fig, bbox_inches="tight", facecolor=fig.get_facecolor())
            plt.close(fig)

        # Metadata
        info = pdf.infodict()
        info["Title"]   = f"Polymarket Bot — Informe Semanal {now_str}"
        info["Author"]  = "Polymarket Bot"
        info["Subject"] = "Weekly Trading Report"

    log.info("[PDF] Informe semanal generado: %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def send_pdf_telegram(
    token: str,
    chat_id: str,
    pdf_path: str,
    caption: str = "",
) -> bool:
    """Sends a PDF file via Telegram sendDocument. Returns True on success."""
    try:
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={
                    "chat_id":    chat_id,
                    "caption":    caption,
                    "parse_mode": "Markdown",
                },
                files={"document": (os.path.basename(pdf_path), f, "application/pdf")},
                timeout=30,
            )
        resp.raise_for_status()
        log.info("[PDF] PDF enviado a Telegram.")
        return True
    except Exception as exc:
        log.error("[PDF] Error al enviar PDF a Telegram: %s", exc)
        return False
