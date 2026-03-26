"""
Telegram command handler — listens for bot commands via long-polling.

Commands:
  /status  — shows current bot state (equity, uptime, paused/active)
  /balance — shows open positions and estimated P&L
  /pause   — pauses all new BUY entries immediately
  /resume  — resumes trading after a manual /pause

Security: only processes messages from the configured TELEGRAM_CHAT_ID.

Usage:
    handler = TelegramCommandHandler(token, chat_id)
    handler.start(bot_instance)   # call once after bot.__init__
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import requests

from logger import get_logger

log = get_logger()

POLL_TIMEOUT = 30   # seconds for Telegram long-poll


class TelegramCommandHandler:

    def __init__(self, token: str, chat_id: str):
        self._token   = token
        self._chat_id = str(chat_id).strip()
        self._offset  = 0
        self._bot     = None
        self._enabled = bool(token and chat_id)

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #

    def start(self, bot) -> None:
        """Attach bot reference and start background polling thread."""
        if not self._enabled:
            log.info(
                "[TELEGRAM_CMD] TELEGRAM_BOT_TOKEN/CHAT_ID no configurados. "
                "Comandos desactivados."
            )
            return
        self._bot = bot
        t = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="tg-cmd-poll",
        )
        t.start()
        log.info(
            "[TELEGRAM_CMD] Escuchando comandos Telegram: "
            "/status  /balance  /pause  /resume"
        )

    # ------------------------------------------------------------------ #
    # Polling loop
    # ------------------------------------------------------------------ #

    def _poll_loop(self) -> None:
        while True:
            try:
                updates = self._get_updates()
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    self._dispatch(upd)
            except requests.exceptions.ReadTimeout:
                pass    # normal for long-poll
            except Exception as exc:
                log.debug("[TELEGRAM_CMD] Error en poll: %s", exc)
                time.sleep(10)

    def _get_updates(self) -> list[dict]:
        resp = requests.get(
            f"https://api.telegram.org/bot{self._token}/getUpdates",
            params={"offset": self._offset, "timeout": POLL_TIMEOUT},
            timeout=POLL_TIMEOUT + 10,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    def _dispatch(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message") or {}
        if not msg:
            return
        # Security: only respond to our chat
        sender_chat = str(msg.get("chat", {}).get("id", ""))
        if sender_chat != self._chat_id:
            return
        text = msg.get("text", "").strip()
        if   text.startswith("/status"):  self._cmd_status()
        elif text.startswith("/balance"): self._cmd_balance()
        elif text.startswith("/pause"):   self._cmd_pause()
        elif text.startswith("/resume"):  self._cmd_resume()

    # ------------------------------------------------------------------ #
    # Telegram send
    # ------------------------------------------------------------------ #

    def _send(self, text: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={
                    "chat_id":    self._chat_id,
                    "text":       text,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        except Exception as exc:
            log.warning("[TELEGRAM_CMD] No se pudo enviar respuesta: %s", exc)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def _cmd_status(self) -> None:
        bot    = self._bot
        now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        equity = bot._total_equity()
        paused = bot._trading_paused
        n_pos  = len(bot._paper.positions) if (bot.paper_trading and bot._paper) else len(bot._positions)
        uptime_h      = (time.time() - bot._bot_start_time)       / 3600
        since_trade_h = (time.time() - bot._last_trade_timestamp) / 3600

        estado = "PAUSADO" if paused else "ACTIVO"
        lines = [
            f"*Estado del Bot — {now}*",
            "",
            f"Estado:           `{estado}`",
            f"Equity:           `{equity:.2f} USDC`",
            f"Posiciones abiertas: `{n_pos}`",
            f"Uptime:           `{uptime_h:.1f}h`",
            f"Último trade:     `hace {since_trade_h:.1f}h`",
            f"Mercados vigilados:  `{len(bot.markets)}`",
            f"Mercados pausados:   `{len(bot._paused_markets)}`",
            f"Lista negra:         `{len(bot._blacklisted_markets)}`",
            f"Modo:             {'PAPER TRADING' if bot.paper_trading else 'REAL'}",
        ]
        self._send("\n".join(lines))
        log.info("[TELEGRAM_CMD] /status enviado")

    def _cmd_balance(self) -> None:
        bot = self._bot
        now = datetime.now().strftime("%H:%M:%S")

        if bot.paper_trading and bot._paper:
            paper   = bot._paper
            balance = paper.balance
            equity  = bot._total_equity()
            positions = paper.positions
        else:
            self._send("_Comando /balance disponible solo en modo PAPER TRADING._")
            return

        lines = [
            f"*Balance — {now}*",
            "",
            f"Saldo en caja:   `{balance:.2f} USDC`",
            f"Equity total:    `{equity:.2f} USDC`",
            f"Retorno:         `{equity - paper.starting_balance:+.2f} USDC`",
            "",
        ]

        if positions:
            lines.append(f"*Posiciones abiertas ({len(positions)})*")
            lines.append("")
            for tid, pos in positions.items():
                label     = next((m.label for m in bot.markets if m.token_id == tid), tid[:12])
                cur_price = bot._latest_prices.get(tid, pos.entry_price)
                pnl_est   = (cur_price - pos.entry_price) * pos.shares
                sign      = "+" if pnl_est >= 0 else ""
                lines += [
                    f"`{label[:24]}`",
                    f"  Entrada: `{pos.entry_price:.4f}` → Actual: `{cur_price:.4f}`",
                    f"  P&L est: `{sign}{pnl_est:.2f} USDC`  |  Tamaño: `{pos.size_usdc:.2f} USDC`",
                    "",
                ]
        else:
            lines.append("_Sin posiciones abiertas._")

        self._send("\n".join(lines))
        log.info("[TELEGRAM_CMD] /balance enviado")

    def _cmd_pause(self) -> None:
        bot = self._bot
        if bot._trading_paused:
            self._send("_El bot ya está pausado. Usa /resume para reactivar._")
            return
        bot._trading_paused = True
        now = datetime.now().strftime("%H:%M:%S")
        self._send(
            f"*Bot PAUSADO* ({now})\n"
            f"No se abrirán nuevas posiciones.\n"
            f"_Usa /resume para reactivar._"
        )
        log.warning("[TELEGRAM_CMD] Trading pausado manualmente via /pause")

    def _cmd_resume(self) -> None:
        bot = self._bot
        if not bot._trading_paused:
            self._send("_El bot ya está activo._")
            return
        bot._trading_paused = False
        now = datetime.now().strftime("%H:%M:%S")
        self._send(
            f"*Bot REACTIVADO* ({now})\n"
            f"Trading reanudado.\n"
            f"_Equity: `{bot._total_equity():.2f} USDC`_"
        )
        log.info("[TELEGRAM_CMD] Trading reanudado via /resume")
