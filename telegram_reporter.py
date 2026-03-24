"""
Telegram daily summary reporter.

Sends a formatted report every 24 hours (configurable) to a Telegram chat
with: current balance, trades made, best/worst trade, and overall performance.

Setup
-----
1. Message @BotFather on Telegram → /newbot → copy the token
2. Send any message to your new bot
3. Get your chat_id:
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
4. Set in .env:
   TELEGRAM_BOT_TOKEN=123456789:AABBccDDee...
   TELEGRAM_CHAT_ID=987654321
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

import requests

from logger import get_logger

log = get_logger()

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# How often to send reports (seconds). Default: 24 hours.
REPORT_INTERVAL_SECONDS = int(os.getenv("REPORT_INTERVAL_SECONDS", str(24 * 3600)))


class TelegramReporter:
    """
    Reads paper_trades.json + optimizer_state.json and sends a
    Markdown-formatted summary to a Telegram chat.
    """

    def __init__(self, trades_file: str = "paper_trades.json"):
        self.token    = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id  = os.getenv("TELEGRAM_CHAT_ID",   "")
        self.trades_file = trades_file
        self._last_report: float = 0.0
        self._enabled = bool(self.token and self.chat_id)

        if not self._enabled:
            log.info(
                "[TELEGRAM] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados. "
                "Reportes desactivados."
            )
        else:
            log.info("[TELEGRAM] Reporter configurado. Primer reporte en %dh.",
                     REPORT_INTERVAL_SECONDS // 3600)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def should_report(self) -> bool:
        return self._enabled and (time.time() - self._last_report) >= REPORT_INTERVAL_SECONDS

    def send_daily_report(
        self,
        starting_balance: float,
        current_prices: dict[str, float] | None = None,
    ):
        """Builds and sends the daily report."""
        self._last_report = time.time()

        if not self._enabled:
            return

        data = self._load_trades()
        message = self._build_message(data, starting_balance, current_prices or {})

        try:
            self._send(message)
            log.info("[TELEGRAM] Reporte diario enviado correctamente.")
        except Exception as exc:
            log.error("[TELEGRAM] Error al enviar reporte: %s", exc)

    def send_alert(self, text: str):
        """Sends a plain-text alert message (e.g. for errors or big trades)."""
        if not self._enabled:
            return
        try:
            self._send(f"*ALERTA*\n{text}")
        except Exception as exc:
            log.warning("[TELEGRAM] No se pudo enviar alerta: %s", exc)

    # -----------------------------------------------------------------------
    # Message builder
    # -----------------------------------------------------------------------

    def _build_message(
        self,
        data: dict,
        starting_balance: float,
        current_prices: dict[str, float],
    ) -> str:
        trades       = data.get("trades", [])
        balance      = data.get("current_balance", starting_balance)
        realized_pnl = data.get("realized_pnl", 0.0)

        # Only look at trades since the last report window
        window_start = time.time() - REPORT_INTERVAL_SECONDS
        recent_trades = [
            t for t in trades
            if _parse_ts(t["timestamp"]) >= window_start
        ]

        # Completed SELL trades (have P&L)
        sells = [t for t in trades if t["action"] == "SELL" and t.get("pnl") is not None]
        recent_sells = [t for t in recent_trades if t["action"] == "SELL" and t.get("pnl") is not None]

        # All-time stats
        total_return     = balance - starting_balance
        total_return_pct = (total_return / starting_balance * 100) if starting_balance else 0
        win_trades       = [t for t in sells if t["pnl"] > 0]
        loss_trades      = [t for t in sells if t["pnl"] <= 0]
        win_rate         = len(win_trades) / len(sells) * 100 if sells else 0

        # Best and worst trade (all time)
        best  = max(sells, key=lambda t: t["pnl"], default=None) if sells else None
        worst = min(sells, key=lambda t: t["pnl"], default=None) if sells else None

        # Optimizer changes (last 24h)
        optimizer_changes = self._recent_optimizer_changes(window_start)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [
            f"*Polymarket Bot - Resumen Diario*",
            f"_{now}_",
            "",
            "*Cuenta*",
            f"  Saldo:      `{balance:.2f} USDC`",
            f"  Inicial:    `{starting_balance:.2f} USDC`",
            f"  P&L total:  `{total_return:+.2f} USDC ({total_return_pct:+.1f}%)`",
            f"  P&L realiz: `{realized_pnl:+.2f} USDC`",
            "",
            "*Operaciones (ultimas 24h)*",
            f"  Trades:     {len(recent_trades)} ({len(recent_sells)} cerradas)",
            f"  Victorias:  {len([t for t in recent_sells if t['pnl']>0])} / {len(recent_sells)}" if recent_sells else "  Sin trades cerradas",
            "",
            "*Historico global*",
            f"  Win rate:   {win_rate:.0f}%  ({len(win_trades)}W / {len(loss_trades)}L)",
        ]

        if best:
            bp = best['pnl'] / best['size_usdc'] * 100 if best['size_usdc'] else 0
            lines.append(f"  Mejor:      `{best['label'][:20]}` +{best['pnl']:.2f} USDC (+{bp:.1f}%)")

        if worst:
            wp = worst['pnl'] / worst['size_usdc'] * 100 if worst['size_usdc'] else 0
            lines.append(f"  Peor:       `{worst['label'][:20]}` {worst['pnl']:.2f} USDC ({wp:.1f}%)")

        # Optimizer summary
        if optimizer_changes:
            lines += ["", f"*Optimizaciones (ultimas 24h)*"]
            for c in optimizer_changes[:3]:  # cap at 3 to avoid wall of text
                lines.append(
                    f"  [{c['verdict'].upper()}] `{c['label'][:20]}` "
                    f"buy {c['old_buy_below']:.4f}->{c['new_buy_below']:.4f} | "
                    f"sell {c['old_sell_above']:.4f}->{c['new_sell_above']:.4f}"
                )

        lines += ["", "_Modo: PAPER TRADING_"]
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _send(self, text: str):
        url  = TELEGRAM_API.format(token=self.token)
        resp = requests.post(
            url,
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()

    def _load_trades(self) -> dict:
        try:
            with open(self.trades_file, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"trades": [], "current_balance": 0, "realized_pnl": 0}

    def _recent_optimizer_changes(self, since_ts: float) -> list[dict]:
        try:
            with open("optimizer_state.json", encoding="utf-8") as f:
                state = json.load(f)
            changes = state.get("changes", [])
            return [c for c in changes if _parse_ts(c["timestamp"]) >= since_ts]
        except (FileNotFoundError, json.JSONDecodeError):
            return []


def _parse_ts(ts_str: str) -> float:
    """Converts 'YYYY-MM-DD HH:MM:SS' to a Unix timestamp."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0
