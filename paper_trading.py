"""
Paper trading engine.

Simulates orders against real market prices without touching the CLOB API.
Tracks a virtual USDC balance, open positions, trade history, and P&L.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from logger import get_logger

log = get_logger()

TRADE_LOG_FILE = "paper_trades.json"


@dataclass
class PaperTrade:
    timestamp: str
    action: str       # "BUY" | "SELL"
    label: str
    token_id: str
    price: float
    shares: float
    size_usdc: float
    pnl: Optional[float] = None   # Only set on SELL


@dataclass
class PaperPosition:
    token_id: str
    label: str
    entry_price: float
    shares: float
    size_usdc: float


class PaperTrader:
    """
    Simulates a trading account with a virtual USDC balance.

    Drop-in replacement for real order execution: call simulate_buy / simulate_sell
    wherever place_limit_order would be called.
    """

    def __init__(self, starting_balance: float):
        self.starting_balance = starting_balance
        self.balance = starting_balance          # Available USDC
        self.positions: dict[str, PaperPosition] = {}
        self.trade_log: list[PaperTrade] = []
        self.realized_pnl: float = 0.0

        log.info("=" * 60)
        log.info("  PAPER TRADING MODE ACTIVO")
        log.info("  Saldo virtual inicial: %.2f USDC", starting_balance)
        log.info("  Ninguna orden real será ejecutada.")
        log.info("=" * 60)

    # -----------------------------------------------------------------------
    # Simulate orders
    # -----------------------------------------------------------------------

    def simulate_buy(
        self,
        token_id: str,
        label: str,
        price: float,
        size_usdc: float,
    ) -> bool:
        """
        Simulates a BUY order.
        Returns True if the trade was accepted (sufficient balance).
        """
        if size_usdc > self.balance:
            log.warning(
                "[PAPER][%s] Saldo insuficiente: necesitas %.2f USDC, tienes %.2f USDC",
                label, size_usdc, self.balance,
            )
            return False

        if token_id in self.positions:
            log.warning("[PAPER][%s] Ya hay una posición abierta, ignorando BUY", label)
            return False

        shares = round(size_usdc / price, 4)
        self.balance -= size_usdc
        self.positions[token_id] = PaperPosition(
            token_id=token_id,
            label=label,
            entry_price=price,
            shares=shares,
            size_usdc=size_usdc,
        )

        trade = PaperTrade(
            timestamp=_now(),
            action="BUY",
            label=label,
            token_id=token_id,
            price=price,
            shares=shares,
            size_usdc=size_usdc,
        )
        self.trade_log.append(trade)
        self._save_log()

        log.info(
            "[PAPER] COMPRA SIMULADA | %s | precio=%.4f | %.4f acciones | %.2f USDC | saldo=%.2f USDC",
            label, price, shares, size_usdc, self.balance,
        )
        return True

    def simulate_sell(
        self,
        token_id: str,
        label: str,
        price: float,
    ) -> Optional[float]:
        """
        Simulates a SELL order for a full open position.
        Returns the realized P&L, or None if there was no position.
        """
        pos = self.positions.get(token_id)
        if pos is None:
            log.warning("[PAPER][%s] No hay posición abierta para vender", label)
            return None

        proceeds = round(pos.shares * price, 4)
        pnl = proceeds - pos.size_usdc
        pnl_pct = (pnl / pos.size_usdc) * 100

        self.balance += proceeds
        self.realized_pnl += pnl
        del self.positions[token_id]

        trade = PaperTrade(
            timestamp=_now(),
            action="SELL",
            label=label,
            token_id=token_id,
            price=price,
            shares=pos.shares,
            size_usdc=proceeds,
            pnl=round(pnl, 4),
        )
        self.trade_log.append(trade)
        self._save_log()

        pnl_sign = "+" if pnl >= 0 else ""
        log.info(
            "[PAPER] VENTA SIMULADA  | %s | precio=%.4f | P&L=%s%.2f USDC (%s%.1f%%) | saldo=%.2f USDC",
            label, price, pnl_sign, pnl, pnl_sign, pnl_pct, self.balance,
        )
        return pnl

    # -----------------------------------------------------------------------
    # P&L
    # -----------------------------------------------------------------------

    def unrealized_pnl(self, current_prices: dict[str, float]) -> float:
        """Returns total unrealized P&L across all open positions."""
        total = 0.0
        for token_id, pos in self.positions.items():
            price = current_prices.get(token_id)
            if price is not None:
                total += (pos.shares * price) - pos.size_usdc
        return total

    def total_equity(self, current_prices: dict[str, float]) -> float:
        """Virtual balance + mark-to-market value of open positions."""
        open_value = sum(
            pos.shares * current_prices.get(pos.token_id, pos.entry_price)
            for pos in self.positions.values()
        )
        return self.balance + open_value

    # -----------------------------------------------------------------------
    # Reporting
    # -----------------------------------------------------------------------

    def print_summary(self, current_prices: dict[str, float] | None = None):
        """Prints a full paper trading account summary."""
        prices = current_prices or {}
        u_pnl = self.unrealized_pnl(prices)
        equity = self.total_equity(prices)
        total_return = equity - self.starting_balance
        total_return_pct = (total_return / self.starting_balance) * 100

        log.info("=" * 60)
        log.info("  RESUMEN PAPER TRADING")
        log.info("  Saldo inicial:       %.2f USDC", self.starting_balance)
        log.info("  Saldo disponible:    %.2f USDC", self.balance)
        log.info("  P&L realizado:       %+.2f USDC", self.realized_pnl)
        log.info("  P&L no realizado:    %+.2f USDC", u_pnl)
        log.info("  Equity total:        %.2f USDC", equity)
        log.info("  Retorno total:       %+.2f USDC (%+.1f%%)", total_return, total_return_pct)
        log.info("  Operaciones totales: %d", len(self.trade_log))
        log.info("-" * 60)

        if self.positions:
            log.info("  POSICIONES ABIERTAS:")
            for token_id, pos in self.positions.items():
                curr = prices.get(token_id, pos.entry_price)
                upnl = (pos.shares * curr) - pos.size_usdc
                upnl_pct = (upnl / pos.size_usdc) * 100
                log.info(
                    "    [%s] entrada=%.4f | actual=%.4f | P&L=%+.2f USDC (%+.1f%%)",
                    pos.label, pos.entry_price, curr, upnl, upnl_pct,
                )
        else:
            log.info("  Sin posiciones abiertas.")

        log.info("=" * 60)

    def print_trade_history(self):
        """Prints the full list of simulated trades."""
        if not self.trade_log:
            log.info("[PAPER] Sin operaciones registradas aún.")
            return

        log.info("=" * 60)
        log.info("  HISTORIAL DE OPERACIONES (paper)")
        log.info("=" * 60)
        for t in self.trade_log:
            pnl_str = f" | P&L={t.pnl:+.2f} USDC" if t.pnl is not None else ""
            log.info(
                "  %s  %s  %-20s  precio=%.4f  %.4f acciones  %.2f USDC%s",
                t.timestamp, t.action, t.label[:20], t.price, t.shares, t.size_usdc, pnl_str,
            )
        log.info("=" * 60)

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _save_log(self):
        """Saves trade log to JSON file."""
        try:
            data = {
                "starting_balance": self.starting_balance,
                "current_balance": self.balance,
                "realized_pnl": self.realized_pnl,
                "trades": [asdict(t) for t in self.trade_log],
            }
            with open(TRADE_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            log.warning("No se pudo guardar el log de paper trading: %s", exc)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
