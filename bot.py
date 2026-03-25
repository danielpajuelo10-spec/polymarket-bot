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
from sentiment import SentimentAnalyzer
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
    news_query: str = ""    # Google News RSS search query for sentiment filter


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

        # News sentiment analyser
        self._sentiment = SentimentAnalyzer()

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
                    "[%s] Exposicion maxima alcanzada (%.2f USDC), no se abre posicion",
                    market.label, config.MAX_TOTAL_EXPOSURE_USDC,
                )
                return

            if self._has_open_position(market.token_id):
                log.info("[%s] Ya hay posicion abierta, esperando", market.label)
                return

            # Sentiment filter: block BUY if news is clearly bearish
            if market.news_query and not self._sentiment.should_buy(market.news_query):
                log.info("[%s] BUY bloqueado por sentimiento negativo en noticias.", market.label)
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
            # Sentiment filter: hold if news is clearly bullish (let winners run)
            if market.news_query and not self._sentiment.should_sell(market.news_query):
                log.info("[%s] SELL retenido — noticias positivas, manteniendo posicion.", market.label)
                return

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
# Punto de entrada — Mercados activos (actualizado 2026-03-25)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    markets_to_watch = [

        # 1. SPORTS — OKC Thunder wins 2026 NBA Finals (YES, ~38.5 cents)
        #    Near 50/50 market — most tradeable. $151K vol/day, $292K liquidity.
        MarketConfig(
            token_id="49500299856831034491021962156746701298730459370557900271970866855042624695770",
            label="OKC Thunder wins NBA Finals",
            strategy="value_threshold",
            buy_below=0.375,
            sell_above=0.408,
            size_usdc=10,
            news_query="Oklahoma City Thunder NBA Finals 2026",
        ),

        # 2. FIFA — England wins 2026 World Cup (YES, ~12.9 cents)
        #    $610K vol/day, $1.3M liquidity — most liquid FIFA market.
        MarketConfig(
            token_id="115556263888245616435851357148058235707004733438163639091106356867234218207169",
            label="England wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.120,
            sell_above=0.142,
            size_usdc=10,
            news_query="England FIFA World Cup 2026",
        ),

        # 3. FIFA — Argentina wins 2026 World Cup (YES, ~10.1 cents)
        #    $531K vol/day, $1.1M liquidity.
        MarketConfig(
            token_id="18812649149814341758733697580460697418474693998558159483117100240528657629879",
            label="Argentina wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.093,
            sell_above=0.113,
            size_usdc=10,
            news_query="Argentina FIFA World Cup 2026",
        ),

        # 4. FIFA — Brazil wins 2026 World Cup (YES, ~8.7 cents)
        #    $239K vol/day, $926K liquidity.
        MarketConfig(
            token_id="27576533317283401577758999384642760405921738493660383550832555714312627457443",
            label="Brazil wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.079,
            sell_above=0.099,
            size_usdc=10,
            news_query="Brazil FIFA World Cup 2026",
        ),

    ]

    bot = PolymarketBot(markets=markets_to_watch)
    bot.run()
