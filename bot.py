"""
Polymarket Trading Bot
======================
Bot de trading automático para Polymarket usando la CLOB API.

Uso:
    python bot.py

Configuración:
    Edita markets_to_watch y strategy_config al final del archivo,
    o pásalos como argumentos al crear la instancia de PolymarketBot.
"""

from __future__ import annotations

import time
import signal
import sys
from dataclasses import dataclass, field
from typing import Optional

import config
from client import (
    build_client,
    get_midpoint,
    get_positions,
    get_open_orders,
    place_limit_order,
    cancel_order,
    get_markets,
)
from strategy import (
    Signal,
    value_threshold_strategy,
    MeanReversionStrategy,
    check_exit_conditions,
)
from paper_trading import PaperTrader
from optimizer import StrategyOptimizer
from telegram_reporter import TelegramReporter
from logger import get_logger

log = get_logger()


# ---------------------------------------------------------------------------
# Configuración de mercados a vigilar
# ---------------------------------------------------------------------------

@dataclass
class MarketConfig:
    """Define un mercado que el bot debe monitorizar."""
    token_id: str           # ID del token YES (o NO) del mercado
    label: str              # Nombre legible del mercado
    strategy: str           # "value_threshold" | "mean_reversion"
    buy_below: float = 0.35
    sell_above: float = 0.65
    size_usdc: float = None  # None = usa MAX_ORDER_SIZE_USDC de config


# ---------------------------------------------------------------------------
# Estado interno de una posición abierta
# ---------------------------------------------------------------------------

@dataclass
class Position:
    token_id: str
    entry_price: float
    shares: float
    size_usdc: float
    side: str = "BUY"


# ---------------------------------------------------------------------------
# Bot principal
# ---------------------------------------------------------------------------

class PolymarketBot:
    def __init__(self, markets: list[MarketConfig]):
        self.markets = markets
        self.client = build_client()
        self.running = False
        self.paper_trading = config.PAPER_TRADING

        # Paper trader (solo activo cuando PAPER_TRADING=true)
        self._paper: PaperTrader | None = (
            PaperTrader(config.PAPER_TRADING_BALANCE) if self.paper_trading else None
        )

        # Posiciones abiertas rastreadas por el bot (solo en modo real)
        self._positions: dict[str, Position] = {}

        # Estrategias de mean reversion por token
        self._mr_strategies: dict[str, MeanReversionStrategy] = {}

        # Self-optimization engine
        self._optimizer = StrategyOptimizer()

        # Telegram daily reporter
        self._reporter = TelegramReporter()

        # Manejador de señales para Ctrl+C limpio
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, *_):
        log.warning("Senal de parada recibida. Cerrando bot...")
        self.running = False
        if self._paper:
            prices = self._get_current_prices()
            self._paper.print_summary(prices)
            self._paper.print_trade_history()
            equity = self._paper.total_equity(prices)
            ret_pct = (equity - self._paper.starting_balance) / self._paper.starting_balance * 100
            self._reporter.send_alert(
                f"*Bot detenido*\n"
                f"Equity final: `{equity:.2f} USDC`\n"
                f"Retorno total: `{ret_pct:+.1f}%`\n"
                f"Trades realizados: `{len(self._paper.trade_log)}`"
            )
        self._optimizer.print_summary()

    # -----------------------------------------------------------------------
    # Lógica por mercado
    # -----------------------------------------------------------------------

    def _get_signal(self, market: MarketConfig, price: float) -> Signal:
        """Aplica la estrategia configurada para el mercado."""
        if market.strategy == "value_threshold":
            return value_threshold_strategy(
                token_id=market.token_id,
                current_price=price,
                buy_below=market.buy_below,
                sell_above=market.sell_above,
                size_usdc=market.size_usdc,
            )
        elif market.strategy == "mean_reversion":
            if market.token_id not in self._mr_strategies:
                self._mr_strategies[market.token_id] = MeanReversionStrategy()
            mr = self._mr_strategies[market.token_id]
            mr.update(price)
            return mr.evaluate(market.token_id, price)
        else:
            log.warning("Estrategia desconocida '%s', usando HOLD", market.strategy)
            return Signal("HOLD", market.token_id, price, 0)

    def _get_current_prices(self) -> dict[str, float]:
        """Fetches current midpoint prices for all watched markets."""
        prices = {}
        for m in self.markets:
            p = get_midpoint(self.client, m.token_id)
            if p is not None:
                prices[m.token_id] = p
        return prices

    def _total_exposure(self) -> float:
        """Suma de USDC invertidos en posiciones abiertas."""
        if self.paper_trading and self._paper:
            return sum(p.size_usdc for p in self._paper.positions.values())
        return sum(p.size_usdc for p in self._positions.values())

    def _has_open_position(self, token_id: str) -> bool:
        if self.paper_trading and self._paper:
            return token_id in self._paper.positions
        return token_id in self._positions

    def _process_market(self, market: MarketConfig):
        """Ciclo de evaluación para un mercado."""
        price = get_midpoint(self.client, market.token_id)
        if price is None:
            log.warning("[%s] Sin precio disponible, saltando", market.label)
            return

        log.info("[%s] Precio actual: %.4f", market.label, price)

        # 1. Comprobar exit conditions si hay posición abierta
        if self._has_open_position(market.token_id):
            entry_price = (
                self._paper.positions[market.token_id].entry_price
                if self.paper_trading and self._paper
                else self._positions[market.token_id].entry_price
            )
            size_usdc = (
                self._paper.positions[market.token_id].size_usdc
                if self.paper_trading and self._paper
                else self._positions[market.token_id].size_usdc
            )
            exit_signal = check_exit_conditions(
                token_id=market.token_id,
                entry_price=entry_price,
                current_price=price,
                size_usdc=size_usdc,
            )
            if exit_signal.action == "SELL":
                log.info("[%s] %s", market.label, exit_signal.reason)
                if self.paper_trading and self._paper:
                    pnl = self._paper.simulate_sell(market.token_id, market.label, price)
                    if pnl is not None:
                        self._reporter.send_trade_alert(
                            "SELL", market.label, price, size_usdc,
                            self._paper.balance, pnl=pnl, paper=True,
                        )
                else:
                    result = place_limit_order(
                        self.client, market.token_id, "SELL", price, size_usdc
                    )
                    if result:
                        del self._positions[market.token_id]
                        self._reporter.send_trade_alert(
                            "SELL", market.label, price, size_usdc,
                            balance=0, pnl=None, paper=False,
                        )
                return

        # 2. Evaluar señal de entrada
        signal_ = self._get_signal(market, price)
        log.debug("[%s] Señal: %s – %s", market.label, signal_.action, signal_.reason)

        if signal_.action == "BUY":
            size = signal_.size_usdc or config.MAX_ORDER_SIZE_USDC

            if self._total_exposure() + size > config.MAX_TOTAL_EXPOSURE_USDC:
                log.warning(
                    "[%s] Exposición máxima alcanzada (%.2f USDC), no se abre posición",
                    market.label, config.MAX_TOTAL_EXPOSURE_USDC,
                )
                return

            if self._has_open_position(market.token_id):
                log.info("[%s] Ya hay posición abierta, esperando", market.label)
                return

            log.info("[%s] COMPRANDO | Razon: %s", market.label, signal_.reason)
            if self.paper_trading and self._paper:
                ok = self._paper.simulate_buy(market.token_id, market.label, price, size)
                if ok:
                    self._reporter.send_trade_alert(
                        "BUY", market.label, price, size,
                        self._paper.balance, paper=True,
                    )
            else:
                result = place_limit_order(self.client, market.token_id, "BUY", price, size)
                if result:
                    self._positions[market.token_id] = Position(
                        token_id=market.token_id,
                        entry_price=price,
                        shares=round(size / price, 2),
                        size_usdc=size,
                        side="BUY",
                    )
                    self._reporter.send_trade_alert(
                        "BUY", market.label, price, size,
                        balance=0, paper=False,
                    )

        elif signal_.action == "SELL" and self._has_open_position(market.token_id):
            log.info("[%s] VENDIENDO | Razon: %s", market.label, signal_.reason)
            if self.paper_trading and self._paper:
                pos = self._paper.positions[market.token_id]
                pnl = self._paper.simulate_sell(market.token_id, market.label, price)
                if pnl is not None:
                    self._reporter.send_trade_alert(
                        "SELL", market.label, price, pos.size_usdc,
                        self._paper.balance, pnl=pnl, paper=True,
                    )
            else:
                pos = self._positions[market.token_id]
                result = place_limit_order(
                    self.client, market.token_id, "SELL", price, pos.size_usdc
                )
                if result:
                    del self._positions[market.token_id]
                    self._reporter.send_trade_alert(
                        "SELL", market.label, price, pos.size_usdc,
                        balance=0, pnl=None, paper=False,
                    )

        else:
            log.info("[%s] HOLD | %s", market.label, signal_.reason)

    # -----------------------------------------------------------------------
    # Loop principal
    # -----------------------------------------------------------------------

    def print_status(self):
        """Imprime un resumen del estado actual del bot."""
        if self.paper_trading and self._paper:
            prices = self._get_current_prices()
            self._paper.print_summary(prices)
            return

        exposure = self._total_exposure()
        log.info("=" * 60)
        log.info("ESTADO DEL BOT")
        log.info("  Posiciones abiertas: %d", len(self._positions))
        log.info("  Exposición total:    %.2f / %.2f USDC", exposure, config.MAX_TOTAL_EXPOSURE_USDC)
        for token_id, pos in self._positions.items():
            label = next((m.label for m in self.markets if m.token_id == token_id), token_id[:12])
            log.info(
                "  [%s] entrada=%.3f | %.2f acciones | %.2f USDC",
                label, pos.entry_price, pos.shares, pos.size_usdc,
            )
        log.info("=" * 60)

    def run(self):
        """Inicia el loop principal del bot."""
        mode = "PAPER TRADING (sin dinero real)" if self.paper_trading else "REAL"
        log.info("Bot iniciado en modo %s. Monitoreando %d mercados.", mode, len(self.markets))
        log.info("Intervalo: %d segundos | Red: %s", config.LOOP_INTERVAL_SECONDS, config.NETWORK)
        self.running = True

        balance = self._paper.balance if self._paper else 0
        market_names = ", ".join(m.label for m in self.markets)
        self._reporter.send_alert(
            f"*Bot iniciado* ({'PAPER' if self.paper_trading else 'REAL'})\n"
            f"Mercados: {market_names}\n"
            f"Saldo: `{balance:.2f} USDC`"
        )

        iteration = 0
        while self.running:
            iteration += 1
            log.info("--- Iteración #%d ---", iteration)

            for market in self.markets:
                try:
                    self._process_market(market)
                except Exception as exc:
                    log.error("[%s] Error inesperado: %s", market.label, exc)

            if iteration % 5 == 0:
                self.print_status()

            # Run self-optimization every 24h (or OPTIMIZE_INTERVAL_SECONDS)
            if self._optimizer.should_run():
                self._optimizer.run(self.markets)

            # Send Telegram daily report
            if self._reporter.should_report():
                prices = self._get_current_prices()
                starting = (
                    self._paper.starting_balance
                    if self._paper
                    else config.PAPER_TRADING_BALANCE
                )
                self._reporter.send_daily_report(starting, prices)

            if self.running:
                log.info("Esperando %d segundos...", config.LOOP_INTERVAL_SECONDS)
                time.sleep(config.LOOP_INTERVAL_SECONDS)

        log.info("Bot detenido correctamente.")


# ---------------------------------------------------------------------------
# Punto de entrada — EDITA AQUÍ TUS MERCADOS
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    markets_to_watch = [

        # 1. FIFA — Spain wins 2026 World Cup (YES, ~15 cents)
        #    Buy if dips under 12 cents, sell above 25 cents.
        MarketConfig(
            token_id="4394372887385518214471608448209527405727552777602031099972143344338178308080",
            label="Spain wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.12,
            sell_above=0.25,
            size_usdc=10,
        ),

        # 2. CRYPTO — Bitcoin hits $1M before GTA VI release (YES, ~49 cents)
        #    Near 50/50 market; buy dips under 40 cents, sell above 65 cents.
        MarketConfig(
            token_id="105267568073659068217311993901927962476298440625043565106676088842803600775810",
            label="Bitcoin $1M before GTA VI",
            strategy="value_threshold",
            buy_below=0.40,
            sell_above=0.65,
            size_usdc=10,
        ),

        # 3. US POLITICS — Jasmine Crockett wins 2028 Dem nomination (YES, ~0.75 cents)
        #    Low probability market; buy dips under 0.5 cents, sell above 2 cents.
        MarketConfig(
            token_id="22103094389913052942362639589409218272323168761614999702665821259175535456835",
            label="Crockett wins 2028 Dem nom",
            strategy="value_threshold",
            buy_below=0.005,
            sell_above=0.02,
            size_usdc=10,
        ),

        # 4. AI/TECH — Elon Musk wins 2028 US Presidential Election (YES, ~1 cent)
        #    Speculative tech-adjacent market; buy under 0.8 cents, sell above 3 cents.
        MarketConfig(
            token_id="26641906520532802078452346454133721131611596169940893262820937050881742190686",
            label="Elon Musk wins 2028 election",
            strategy="value_threshold",
            buy_below=0.008,
            sell_above=0.03,
            size_usdc=10,
        ),

    ]

    bot = PolymarketBot(markets=markets_to_watch)
    bot.run()
