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

        # Manejador de señales para Ctrl+C limpio
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, *_):
        log.warning("Señal de parada recibida. Cerrando bot...")
        self.running = False
        if self._paper:
            prices = self._get_current_prices()
            self._paper.print_summary(prices)
            self._paper.print_trade_history()

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
                    self._paper.simulate_sell(market.token_id, market.label, price)
                else:
                    result = place_limit_order(
                        self.client, market.token_id, "SELL", price, size_usdc
                    )
                    if result:
                        del self._positions[market.token_id]
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

            log.info("[%s] COMPRANDO | Razón: %s", market.label, signal_.reason)
            if self.paper_trading and self._paper:
                self._paper.simulate_buy(market.token_id, market.label, price, size)
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

        elif signal_.action == "SELL" and self._has_open_position(market.token_id):
            log.info("[%s] VENDIENDO | Razón: %s", market.label, signal_.reason)
            if self.paper_trading and self._paper:
                self._paper.simulate_sell(market.token_id, market.label, price)
            else:
                pos = self._positions[market.token_id]
                result = place_limit_order(
                    self.client, market.token_id, "SELL", price, pos.size_usdc
                )
                if result:
                    del self._positions[market.token_id]

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

            if self.running:
                log.info("Esperando %d segundos...", config.LOOP_INTERVAL_SECONDS)
                time.sleep(config.LOOP_INTERVAL_SECONDS)

        log.info("Bot detenido correctamente.")


# ---------------------------------------------------------------------------
# Punto de entrada — EDITA AQUÍ TUS MERCADOS
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    markets_to_watch = [
        # Ejemplo: Mercado de elecciones de EE.UU.
        # Para encontrar el token_id ve a un mercado en Polymarket,
        # abre DevTools y busca la petición a la CLOB API, o usa:
        # python -c "from client import get_markets; import json; print(json.dumps(get_markets(5), indent=2))"

        MarketConfig(
            token_id="REEMPLAZA_CON_TOKEN_ID_REAL",   # <-- Token ID del mercado
            label="Mi primer mercado",
            strategy="value_threshold",
            buy_below=0.30,    # Compra si el precio es menor de 30 cents
            sell_above=0.70,   # Vende si el precio supera 70 cents
            size_usdc=20,      # Usa 20 USDC por operación
        ),

        # Descomenta para añadir más mercados:
        # MarketConfig(
        #     token_id="OTRO_TOKEN_ID",
        #     label="Otro mercado",
        #     strategy="mean_reversion",
        #     size_usdc=15,
        # ),
    ]

    bot = PolymarketBot(markets=markets_to_watch)
    bot.run()
