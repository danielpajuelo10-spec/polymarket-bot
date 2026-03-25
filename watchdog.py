"""
Polymarket Bot Watchdog
=======================
Runs bot.py as a managed subprocess and automatically restarts it if it crashes.

Usage:
    python watchdog.py          # starts and monitors bot.py
    python watchdog.py --once   # single run, no restart (for debugging)

Behaviour:
  - If bot.py exits with code 0 (clean shutdown via Ctrl+C / SIGTERM):
      Watchdog also stops — the user intentionally shut down the bot.
  - If bot.py exits with any non-zero code (crash / exception):
      Sends a Telegram alert and restarts after RESTART_DELAY_SECONDS.
  - Safety valve: if the bot crashes more than MAX_RESTARTS times in
      RESTART_WINDOW_SECONDS, the watchdog stops and sends a final alert.
"""

from __future__ import annotations

import os
import sys
import signal
import subprocess
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESTART_DELAY_SECONDS  = int(os.getenv("WATCHDOG_RESTART_DELAY",  "30"))
MAX_RESTARTS           = int(os.getenv("WATCHDOG_MAX_RESTARTS",   "10"))
RESTART_WINDOW_SECONDS = int(os.getenv("WATCHDOG_RESTART_WINDOW", str(60 * 60)))  # 1h

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_log.txt")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    line = f"{_now()} [WATCHDOG] {msg}"
    print(line, flush=True)
    # Also append to bot log so everything is in one place
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _telegram(text: str):
    if not TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        _log(f"No se pudo enviar alerta Telegram: {exc}")


# ---------------------------------------------------------------------------
# Main watchdog loop
# ---------------------------------------------------------------------------

def run():
    _log("Watchdog iniciado. Monitoreando bot.py...")
    _telegram(
        f"*Watchdog iniciado*\n"
        f"Monitoreando `bot.py` — reinicio automatico activado.\n"
        f"Max reinicios: `{MAX_RESTARTS}` en `{RESTART_WINDOW_SECONDS // 3600}h`"
    )

    restart_times: list[float] = []
    single_run = "--once" in sys.argv

    while True:
        _log(f"Lanzando bot.py (Python: {sys.executable})")

        with open(LOG_FILE, "a", encoding="utf-8") as log_fh:
            process = subprocess.Popen(
                [sys.executable, BOT_SCRIPT],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )

        # Forward Ctrl+C to the child so it can shut down cleanly
        def _forward_signal(sig, frame):
            _log("Señal de parada recibida — deteniendo bot...")
            process.send_signal(sig)

        signal.signal(signal.SIGINT,  _forward_signal)
        signal.signal(signal.SIGTERM, _forward_signal)

        start_ts = time.time()
        process.wait()
        uptime   = int(time.time() - start_ts)
        code     = process.returncode

        _log(f"bot.py terminó | código={code} | uptime={uptime}s")

        # Clean exit (code 0) — user intentionally stopped the bot
        if code == 0 or single_run:
            _log("Salida limpia. Watchdog detenido.")
            _telegram("*Watchdog detenido*\nbot.py cerró correctamente (código 0).")
            break

        # ----- Crash detected -----
        now = time.time()
        restart_times = [t for t in restart_times if now - t < RESTART_WINDOW_SECONDS]

        if len(restart_times) >= MAX_RESTARTS:
            msg = (
                f"*WATCHDOG: Demasiados reinicios*\n"
                f"`{MAX_RESTARTS}` crashes en `{RESTART_WINDOW_SECONDS // 3600}h`.\n"
                f"Watchdog detenido — revisar logs manualmente."
            )
            _log(msg)
            _telegram(msg)
            break

        msg = (
            f"*WATCHDOG: bot.py se reinicio*\n"
            f"Codigo de salida: `{code}`\n"
            f"Uptime: `{uptime}s`\n"
            f"Reinicio #{len(restart_times) + 1} de {MAX_RESTARTS}\n"
            f"Esperando `{RESTART_DELAY_SECONDS}s` antes de reiniciar..."
        )
        _log(msg)
        _telegram(msg)

        restart_times.append(now)
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    run()
