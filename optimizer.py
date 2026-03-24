"""
Self-optimization engine for the Polymarket bot.

Every 24 hours (configurable), reads paper_trades.json and:
  1. Groups completed round-trips (BUY → SELL) per market
  2. Computes win rate and average P&L %
  3. Adjusts buy_below / sell_above thresholds based on performance
  4. Logs every change with the metric that triggered it

Adjustment logic
----------------
  Poor performance  (win_rate < 40% OR avg_pnl_pct < -5%):
    → Lower buy_below  (-10%): only enter on deeper dips
    → Raise sell_above (+10%): wait for a larger recovery move

  Strong performance (win_rate > 65% AND avg_pnl_pct > +5%):
    → Raise buy_below  (+5%): can afford to enter a bit earlier
    → Lower sell_above (-5%): lock in profits sooner

  Neutral  (everything else): no change — leave thresholds alone

All changes are capped so buy_below stays in [0.001, 0.95] and
sell_above stays in [buy_below + 0.01, 0.99].

Both the current state (thresholds) and a full audit log of every
change are persisted to optimizer_state.json.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import TYPE_CHECKING

from logger import get_logger

if TYPE_CHECKING:
    from bot import MarketConfig

log = get_logger()

STATE_FILE = "optimizer_state.json"

# How often to run the optimizer (seconds). Default: 24 hours.
OPTIMIZE_INTERVAL_SECONDS = int(os.getenv("OPTIMIZE_INTERVAL_SECONDS", str(24 * 3600)))

# Minimum completed round-trips required before adjusting a market.
MIN_TRADES_REQUIRED = 3

# Thresholds that define "poor" vs "strong" performance.
POOR_WIN_RATE   = 0.40
STRONG_WIN_RATE = 0.65
POOR_AVG_PNL_PCT   = -5.0
STRONG_AVG_PNL_PCT = +5.0

# Step sizes for adjustments.
POOR_BUY_STEP    = 0.90   # multiply buy_below by this on poor perf
POOR_SELL_STEP   = 1.10   # multiply sell_above by this on poor perf
STRONG_BUY_STEP  = 1.05   # multiply buy_below by this on strong perf
STRONG_SELL_STEP = 0.95   # multiply sell_above by this on strong perf

# Hard bounds.
BUY_MIN  = 0.001
BUY_MAX  = 0.95
SELL_MIN_MARGIN = 0.01  # sell_above must be at least this above buy_below
SELL_MAX = 0.99


@dataclass
class ChangeRecord:
    timestamp: str
    label: str
    metric: str             # e.g. "win_rate=35%, avg_pnl=-8.2%"
    verdict: str            # "poor" | "strong" | "neutral"
    old_buy_below: float
    new_buy_below: float
    old_sell_above: float
    new_sell_above: float
    reason: str


class StrategyOptimizer:
    """
    Reads paper_trades.json, analyses performance per market,
    and mutates MarketConfig thresholds in place.
    """

    def __init__(self, trades_file: str = "paper_trades.json"):
        self.trades_file = trades_file
        self._last_run: float = 0.0
        self._state: dict = self._load_state()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def should_run(self) -> bool:
        return (time.time() - self._last_run) >= OPTIMIZE_INTERVAL_SECONDS

    def run(self, markets: list[MarketConfig]) -> int:
        """
        Analyses trade history and adjusts thresholds.
        Returns the number of markets whose parameters were changed.
        """
        self._last_run = time.time()
        log.info("[OPTIMIZER] Iniciando revisión de estrategias...")

        trades = self._load_trades()
        if not trades:
            log.info("[OPTIMIZER] Sin historial de trades aún. Nada que optimizar.")
            return 0

        round_trips = self._build_round_trips(trades)
        changed = 0

        for market in markets:
            if market.strategy != "value_threshold":
                continue

            trips = round_trips.get(market.token_id, [])
            if len(trips) < MIN_TRADES_REQUIRED:
                log.info(
                    "[OPTIMIZER] [%s] Solo %d trades completados (mínimo %d). Saltando.",
                    market.label, len(trips), MIN_TRADES_REQUIRED,
                )
                continue

            win_rate, avg_pnl_pct = self._compute_metrics(trips)
            verdict, record = self._adjust(market, win_rate, avg_pnl_pct)

            if record:
                changed += 1
                self._state.setdefault("changes", []).append(asdict(record))
                log.info(
                    "[OPTIMIZER] [%s] %s | %s | buy_below: %.4f->%.4f | sell_above: %.4f->%.4f",
                    market.label, verdict.upper(), record.reason,
                    record.old_buy_below, record.new_buy_below,
                    record.old_sell_above, record.new_sell_above,
                )
            else:
                log.info(
                    "[OPTIMIZER] [%s] NEUTRAL | win_rate=%.0f%% avg_pnl=%+.1f%% — sin cambios.",
                    market.label, win_rate * 100, avg_pnl_pct,
                )

        self._save_state()
        log.info("[OPTIMIZER] Revisión completada. %d mercado(s) ajustado(s).", changed)
        return changed

    def print_summary(self):
        """Prints a table of all recorded parameter changes."""
        changes = self._state.get("changes", [])
        if not changes:
            log.info("[OPTIMIZER] Sin cambios de parámetros registrados aún.")
            return

        log.info("=" * 70)
        log.info("  HISTORIAL DE OPTIMIZACIONES")
        log.info("=" * 70)
        for c in changes:
            log.info(
                "  %s  %-25s  %-8s  buy: %.4f->%.4f  sell: %.4f->%.4f",
                c["timestamp"], c["label"][:25], c["verdict"].upper(),
                c["old_buy_below"], c["new_buy_below"],
                c["old_sell_above"], c["new_sell_above"],
            )
            log.info("    Razón: %s", c["reason"])
        log.info("=" * 70)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_round_trips(self, trades: list[dict]) -> dict[str, list[dict]]:
        """
        Pairs BUY and SELL trades by token_id into round-trips.
        Returns {token_id: [{"buy_price": x, "sell_price": y, "pnl": z, ...}, ...]}
        """
        result: dict[str, list[dict]] = {}
        pending: dict[str, dict] = {}  # token_id → pending BUY trade

        for t in trades:
            tid = t["token_id"]
            if t["action"] == "BUY":
                pending[tid] = t
            elif t["action"] == "SELL" and tid in pending:
                buy = pending.pop(tid)
                result.setdefault(tid, []).append({
                    "label":      t["label"],
                    "buy_price":  buy["price"],
                    "sell_price": t["price"],
                    "buy_usdc":   buy["size_usdc"],
                    "sell_usdc":  t["size_usdc"],
                    "pnl":        t.get("pnl", t["size_usdc"] - buy["size_usdc"]),
                })

        return result

    def _compute_metrics(self, trips: list[dict]) -> tuple[float, float]:
        """Returns (win_rate, avg_pnl_pct) for a list of round-trips."""
        wins = sum(1 for t in trips if t["pnl"] > 0)
        win_rate = wins / len(trips)
        avg_pnl_pct = (
            sum(t["pnl"] / t["buy_usdc"] * 100 for t in trips) / len(trips)
        )
        return win_rate, avg_pnl_pct

    def _adjust(
        self,
        market: MarketConfig,
        win_rate: float,
        avg_pnl_pct: float,
    ) -> tuple[str, ChangeRecord | None]:
        """
        Mutates market thresholds and returns (verdict, ChangeRecord|None).
        Returns None for ChangeRecord when verdict is 'neutral'.
        """
        metric_str = f"win_rate={win_rate:.0%}, avg_pnl={avg_pnl_pct:+.1f}%"

        is_poor   = win_rate < POOR_WIN_RATE   or avg_pnl_pct < POOR_AVG_PNL_PCT
        is_strong = win_rate > STRONG_WIN_RATE and avg_pnl_pct > STRONG_AVG_PNL_PCT

        if not is_poor and not is_strong:
            return "neutral", None

        old_buy   = market.buy_below
        old_sell  = market.sell_above

        if is_poor:
            verdict = "poor"
            reason  = (
                f"Rendimiento bajo ({metric_str}): "
                "reduciendo buy_below para entrar solo en caídas más profundas, "
                "subiendo sell_above para esperar mayor recuperación"
            )
            new_buy  = old_buy  * POOR_BUY_STEP
            new_sell = old_sell * POOR_SELL_STEP
        else:
            verdict = "strong"
            reason  = (
                f"Rendimiento sólido ({metric_str}): "
                "subiendo buy_below para entrar algo antes, "
                "bajando sell_above para asegurar ganancias antes"
            )
            new_buy  = old_buy  * STRONG_BUY_STEP
            new_sell = old_sell * STRONG_SELL_STEP

        # Apply hard bounds
        new_buy  = max(BUY_MIN,  min(BUY_MAX,  new_buy))
        new_sell = max(new_buy + SELL_MIN_MARGIN, min(SELL_MAX, new_sell))

        market.buy_below   = round(new_buy,  6)
        market.sell_above  = round(new_sell, 6)

        record = ChangeRecord(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            label=market.label,
            metric=metric_str,
            verdict=verdict,
            old_buy_below=old_buy,
            new_buy_below=market.buy_below,
            old_sell_above=old_sell,
            new_sell_above=market.sell_above,
            reason=reason,
        )
        return verdict, record

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _load_trades(self) -> list[dict]:
        try:
            with open(self.trades_file, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("trades", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"changes": []}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            log.warning("[OPTIMIZER] No se pudo guardar el estado: %s", exc)
