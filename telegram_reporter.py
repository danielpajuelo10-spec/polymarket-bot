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

    Scheduling:
      Daily report  — fires once per day at 08:00 local time.
      Weekly report — fires once per week on Monday at 08:00 local time.
    """

    def __init__(self, trades_file: str = "paper_trades.json"):
        self.token       = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id     = os.getenv("TELEGRAM_CHAT_ID",   "")
        self.trades_file = trades_file
        self._last_report: float        = 0.0
        self._last_weekly_report: float = 0.0
        self._last_health_check: float  = 0.0
        self._enabled = bool(self.token and self.chat_id)

        if not self._enabled:
            log.info(
                "[TELEGRAM] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados. "
                "Reportes desactivados."
            )
        else:
            log.info("[TELEGRAM] Reporter configurado. Reporte diario a las 08:00.")

    # -----------------------------------------------------------------------
    # Scheduling helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _today_at_8am() -> datetime:
        return datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)

    def should_report(self) -> bool:
        """True once per day after 08:00, regardless of bot start time."""
        if not self._enabled:
            return False
        cutoff = self._today_at_8am()
        if datetime.now() < cutoff:
            return False
        last_dt = datetime.fromtimestamp(self._last_report) if self._last_report else datetime.min
        return last_dt < cutoff

    def should_weekly_report(self) -> bool:
        """True once per Monday after 08:00."""
        if not self._enabled:
            return False
        if datetime.now().weekday() != 0:   # 0 = Monday
            return False
        cutoff = self._today_at_8am()
        if datetime.now() < cutoff:
            return False
        last_dt = datetime.fromtimestamp(self._last_weekly_report) if self._last_weekly_report else datetime.min
        return last_dt < cutoff

    def should_health_check(self) -> bool:
        """True once per day after 09:00 (separate from 08:00 daily report)."""
        if not self._enabled:
            return False
        cutoff = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        if datetime.now() < cutoff:
            return False
        last_dt = datetime.fromtimestamp(self._last_health_check) if self._last_health_check else datetime.min
        return last_dt < cutoff

    def send_daily_report(
        self,
        starting_balance: float,
        current_prices: dict[str, float] | None = None,
    ):
        """Builds and sends the daily 08:00 report."""
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

    def send_weekly_report(
        self,
        starting_balance: float,
        current_prices: dict[str, float] | None = None,
    ):
        """Builds and sends the Monday 08:00 weekly summary."""
        self._last_weekly_report = time.time()

        if not self._enabled:
            return

        data      = self._load_trades()
        trades    = data.get("trades", [])
        balance   = data.get("current_balance", starting_balance)

        # Trades from the last 7 days
        week_ago  = time.time() - 7 * 24 * 3600
        week_sells = [
            t for t in trades
            if t["action"] == "SELL"
            and t.get("pnl") is not None
            and _parse_ts(t["timestamp"]) >= week_ago
        ]

        # Per-market P&L this week
        market_pnl: dict[str, float] = {}
        for t in week_sells:
            lbl = t["label"]
            market_pnl[lbl] = market_pnl.get(lbl, 0.0) + t["pnl"]

        best_market  = max(market_pnl, key=market_pnl.get) if market_pnl else None
        worst_market = min(market_pnl, key=market_pnl.get) if market_pnl else None

        wins      = [t for t in week_sells if t["pnl"] > 0]
        win_rate  = len(wins) / len(week_sells) * 100 if week_sells else 0.0
        total_ret = balance - starting_balance
        ret_pct   = total_ret / starting_balance * 100 if starting_balance else 0.0

        now_str = datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"*Resumen Semanal — {now_str}*",
            "",
            f"Retorno total:  `{total_ret:+.2f} USDC ({ret_pct:+.1f}%)`",
            f"Saldo actual:   `{balance:.2f} USDC`",
            f"Trades (7d):    `{len(week_sells)} cerradas`",
            f"Win rate (7d):  `{win_rate:.0f}%`",
            "",
        ]
        if best_market:
            lines.append(f"Mejor mercado:  `{best_market[:28]}` +{market_pnl[best_market]:.2f} USDC")
        if worst_market and worst_market != best_market:
            lines.append(f"Peor mercado:   `{worst_market[:28]}` {market_pnl[worst_market]:.2f} USDC")
        lines.append("_Modo: PAPER TRADING_")

        try:
            self._send("\n".join(lines))
            log.info("[TELEGRAM] Resumen semanal enviado.")
        except Exception as exc:
            log.error("[TELEGRAM] Error al enviar resumen semanal: %s", exc)

    def send_health_check(self, checks: dict[str, bool]) -> None:
        """Sends the daily 09:00 health check status to Telegram."""
        self._last_health_check = time.time()
        if not self._enabled:
            return

        all_ok  = all(checks.values())
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        status  = "OK" if all_ok else "PROBLEMAS DETECTADOS"

        lines = [
            f"*Health Check — {now_str}*",
            f"Estado: `{status}`",
            "",
        ]
        for name, ok in checks.items():
            lines.append(f"{'OK' if ok else 'ERROR'}  `{name}`")

        if not all_ok:
            lines += ["", "_Revisar bot\\_log.txt para detalles._"]

        try:
            self._send("\n".join(lines))
            log.info("[TELEGRAM] Health check enviado — %s", status)
        except Exception as exc:
            log.error("[TELEGRAM] Error al enviar health check: %s", exc)

    def send_trade_alert(
        self,
        action: str,                    # "BUY" or "SELL"
        label: str,
        price: float,
        size_usdc: float,
        balance: float,
        pnl: float | None = None,       # Only for SELL
        paper: bool = True,
        confidence: int | None = None,  # 0-100 confidence score
    ):
        """Sends an instant Telegram notification when a trade is executed."""
        if not self._enabled:
            return

        mode_tag = " PAPER" if paper else " REAL"
        now = datetime.now().strftime("%H:%M:%S")
        conf_line = f"Confianza: `{confidence}/100`" if confidence is not None else None

        if action == "BUY":
            lines = [
                f"*COMPRA{mode_tag}*  _{now}_",
                f"Mercado: `{label}`",
                f"Precio:  `{price:.4f}`",
                f"USDC:    `{size_usdc:.2f}`",
                f"Saldo:   `{balance:.2f} USDC`",
            ]
            if conf_line:
                lines.append(conf_line)
        else:
            pnl_val   = pnl if pnl is not None else 0.0
            pnl_pct   = (pnl_val / size_usdc * 100) if size_usdc else 0.0
            pnl_sign  = "+" if pnl_val >= 0 else ""
            emoji     = "UP" if pnl_val >= 0 else "DOWN"
            lines = [
                f"*VENTA{mode_tag}*  _{now}_  {emoji}",
                f"Mercado: `{label}`",
                f"Precio:  `{price:.4f}`",
                f"P&L:     `{pnl_sign}{pnl_val:.2f} USDC ({pnl_sign}{pnl_pct:.1f}%)`",
                f"Saldo:   `{balance:.2f} USDC`",
            ]

        try:
            self._send("\n".join(lines))
            log.info("[TELEGRAM] Alerta de trade enviada (%s %s).", action, label)
        except Exception as exc:
            log.warning("[TELEGRAM] No se pudo enviar alerta de trade: %s", exc)

    def send_alert(self, text: str):
        """Sends a plain-text alert (errors, bot start/stop, etc.)."""
        if not self._enabled:
            return
        try:
            self._send(text)
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
