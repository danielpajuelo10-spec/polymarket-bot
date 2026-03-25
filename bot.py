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
from collections import deque
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
    mr_window: int = 10     # Mean reversion: lookback window
    mr_std_threshold: float = 0.4  # Mean reversion: z-score trigger
    liquidity_usdc: float = 0      # Known market liquidity for confidence scoring


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

        # Cooldown: timestamp del último BUY ejecutado por token
        self._last_trade_time: dict[str, float] = {}

        # Price history for trend scoring: token_id -> deque of (timestamp, price)
        # Keeps up to 750 entries (~6h25m at 30s interval)
        self._price_history: dict[str, deque] = {}

        # Latest known price per token (used for equity calculations)
        self._latest_prices: dict[str, float] = {}

        # Risk management state
        self._trading_paused: bool = False

        # Hourly backup state
        self._last_backup: float = 0.0

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
                self._mr_strategies[market.token_id] = MeanReversionStrategy(
                    window=market.mr_window,
                    std_threshold=market.mr_std_threshold,
                )
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

    def _total_equity(self) -> float:
        """Current equity = cash balance + mark-to-market value of open positions."""
        if self.paper_trading and self._paper:
            return self._paper.total_equity(self._latest_prices)
        return config.PAPER_TRADING_BALANCE  # real mode: approximate

    def _calculate_confidence(self, market: MarketConfig, price: float) -> tuple[int, str]:
        """
        Returns (score 0-100, human-readable breakdown string).

        Scoring:
          Trend last 6h  — 40 pts
          News sentiment — 40 pts
          Liquidity      — 20 pts
        """
        # ---- 1. Price trend last 6h (0-40 pts) ----
        history = self._price_history.get(market.token_id)
        six_h_ago = time.time() - 6 * 3600

        if history and len(history) >= 5:
            past = [p for ts, p in history if ts >= six_h_ago]
            ref_price = past[0] if past else list(history)[0][1]
            change_pct = (price - ref_price) / ref_price * 100 if ref_price > 0 else 0

            if change_pct > 5:
                trend_pts, trend_desc = 40, f"+{change_pct:.1f}% alza fuerte"
            elif change_pct > 2:
                trend_pts, trend_desc = 30, f"+{change_pct:.1f}% alza"
            elif change_pct > 0:
                trend_pts, trend_desc = 25, f"+{change_pct:.1f}% leve alza"
            elif change_pct > -0.5:
                trend_pts, trend_desc = 20, f"{change_pct:.1f}% estable"
            elif change_pct > -2:
                trend_pts, trend_desc = 15, f"{change_pct:.1f}% leve baja"
            elif change_pct > -5:
                trend_pts, trend_desc = 10, f"{change_pct:.1f}% baja"
            else:
                trend_pts, trend_desc = 5, f"{change_pct:.1f}% baja fuerte"
        else:
            trend_pts, trend_desc = 20, "sin historial (neutral)"

        # ---- 2. News sentiment (0-40 pts) ----
        if market.news_query:
            result = self._sentiment.analyse(market.news_query)
            # Linear map [-1, 1] -> [0, 40]
            sentiment_pts = max(0, min(40, int((result.score + 1) / 2 * 40)))
            sentiment_desc = f"{result.label} ({result.score:+.2f})"
        else:
            sentiment_pts, sentiment_desc = 20, "sin query (neutral)"

        # ---- 3. Market liquidity (0-20 pts) ----
        liq = market.liquidity_usdc
        if liq >= 1_000_000:
            liq_pts, liq_desc = 20, f"${liq/1e6:.1f}M"
        elif liq >= 500_000:
            liq_pts, liq_desc = 16, f"${liq/1e3:.0f}K"
        elif liq >= 200_000:
            liq_pts, liq_desc = 12, f"${liq/1e3:.0f}K"
        elif liq >= 100_000:
            liq_pts, liq_desc = 8,  f"${liq/1e3:.0f}K"
        elif liq >= 50_000:
            liq_pts, liq_desc = 4,  f"${liq/1e3:.0f}K"
        elif liq > 0:
            liq_pts, liq_desc = 2,  f"${liq/1e3:.0f}K"
        else:
            liq_pts, liq_desc = 10, "desconocida"

        total = trend_pts + sentiment_pts + liq_pts
        breakdown = (
            f"Tendencia={trend_pts}/40 ({trend_desc}) | "
            f"Sentimiento={sentiment_pts}/40 ({sentiment_desc}) | "
            f"Liquidez={liq_pts}/20 ({liq_desc})"
        )
        return total, breakdown

    def _check_momentum(self, token_id: str, price: float) -> tuple[bool, str]:
        """
        Returns (True = OK to buy, reason).
        Blocks BUY if price has fallen more than 0.5% over the last 6 hours.
        Allows through when there is not yet enough history.
        """
        history = self._price_history.get(token_id)
        if not history or len(history) < 5:
            return True, "sin historial 6h (permitido)"

        six_h_ago = time.time() - 6 * 3600
        past_6h = [p for ts, p in history if ts >= six_h_ago]
        ref = past_6h[0] if past_6h else list(history)[0][1]

        if ref <= 0:
            return True, "referencia invalida"

        change_pct = (price - ref) / ref * 100
        if change_pct >= -0.5:
            return True, f"{change_pct:+.1f}% en 6h"
        return False, f"{change_pct:.1f}% en 6h (momentum negativo)"

    def _check_24h_drawdown(self, token_id: str, price: float) -> tuple[bool, str]:
        """
        Returns (True = OK to buy, reason).
        Blocks BUY if price has fallen more than MAX_DRAWDOWN_24H_PCT from the
        oldest available price point (up to 24h ago).
        Requires at least 60 data points (~30 min) before activating.
        """
        history = self._price_history.get(token_id)
        if not history or len(history) < 60:
            return True, "sin historial 24h (permitido)"

        oldest_price = list(history)[0][1]
        if oldest_price <= 0:
            return True, "precio base invalido"

        change_pct = (price - oldest_price) / oldest_price * 100
        if change_pct > -config.MAX_DRAWDOWN_24H_PCT:
            return True, f"{change_pct:+.1f}% vs hace 24h"
        return False, f"{change_pct:.1f}% caida en 24h (>{config.MAX_DRAWDOWN_24H_PCT:.0f}% drawdown)"

    def _do_backup(self):
        """Copies paper_trades.json -> paper_trades_backup.json."""
        import shutil
        try:
            shutil.copy("paper_trades.json", "paper_trades_backup.json")
            log.info("[BACKUP] paper_trades_backup.json actualizado")
        except Exception as exc:
            log.warning("[BACKUP] Error al hacer copia de seguridad: %s", exc)

    def _process_market(self, market: MarketConfig):
        """Ciclo de evaluación para un mercado."""
        price = get_midpoint(self.client, market.token_id)
        if price is None:
            log.warning("[%s] Sin precio disponible, saltando", market.label)
            return

        # Update price cache and history for trend scoring
        self._latest_prices[market.token_id] = price
        if market.token_id not in self._price_history:
            self._price_history[market.token_id] = deque(maxlen=2950)  # ~24h at 30s
        self._price_history[market.token_id].append((time.time(), price))

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
                exit_confidence, _ = self._calculate_confidence(market, price)
                if self.paper_trading and self._paper:
                    pnl = self._paper.simulate_sell(market.token_id, market.label, price)
                    if pnl is not None:
                        if pnl <= -config.TRADE_LOSS_ALERT_USDC:
                            self._reporter.send_alert(
                                f"*ALERTA: Perdida elevada en trade*\n"
                                f"Mercado: `{market.label}`\n"
                                f"Perdida: `{pnl:.2f} USDC` (umbral: -{config.TRADE_LOSS_ALERT_USDC:.0f} USDC)\n"
                                f"Razon: _{exit_signal.reason}_"
                            )
                        self._reporter.send_trade_alert(
                            "SELL", market.label, price, size_usdc,
                            self._paper.balance, pnl=pnl, paper=True,
                            confidence=exit_confidence,
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
                            confidence=exit_confidence,
                        )
                return

        # 2. Evaluar señal de entrada
        signal_ = self._get_signal(market, price)
        log.debug("[%s] Señal: %s – %s", market.label, signal_.action, signal_.reason)

        if signal_.action == "BUY":
            # ---- Risk check 1: balance floor ----
            equity = self._total_equity()
            if equity < config.BALANCE_FLOOR_USDC:
                if not self._trading_paused:
                    self._trading_paused = True
                    msg = (
                        f"*ALERTA: Balance bajo, bot pausado*\n"
                        f"Equity actual: `{equity:.2f} USDC`\n"
                        f"Minimo configurado: `{config.BALANCE_FLOOR_USDC:.0f} USDC`"
                    )
                    self._reporter.send_alert(msg)
                    log.warning("[RISK] Equity %.2f < floor %.2f — trading pausado", equity, config.BALANCE_FLOOR_USDC)
                return

            if self._trading_paused:
                log.info("[%s] Bot pausado por balance bajo, saltando BUY", market.label)
                return

            # ---- Risk check 2: position size = min(configured, 5% of equity) ----
            configured_size = market.size_usdc or config.MAX_ORDER_SIZE_USDC
            max_by_pct = round(equity * config.MAX_POSITION_PCT / 100, 2)
            size = min(configured_size, max_by_pct)
            size = max(1.0, size)  # never below 1 USDC

            # ---- Risk check 3: total exposure ≤ 30% of equity ----
            max_exposure = equity * config.MAX_EXPOSURE_PCT / 100
            if self._total_exposure() + size > max_exposure:
                log.warning(
                    "[%s] Exposicion maxima alcanzada (%.0f%% de %.2f USDC), no se abre posicion",
                    market.label, config.MAX_EXPOSURE_PCT, equity,
                )
                return

            if self._has_open_position(market.token_id):
                log.info("[%s] Ya hay posicion abierta, esperando", market.label)
                return

            # ---- Risk check 4: cooldown ----
            last = self._last_trade_time.get(market.token_id, 0)
            elapsed = time.time() - last
            if elapsed < config.MIN_TRADE_INTERVAL_SECONDS:
                remaining_min = (config.MIN_TRADE_INTERVAL_SECONDS - elapsed) / 60
                log.info(
                    "[%s] Cooldown activo — faltan %.0f min para siguiente entrada",
                    market.label, remaining_min,
                )
                return

            # ---- Filter 5: liquidity floor ----
            if market.liquidity_usdc > 0 and market.liquidity_usdc < config.MIN_LIQUIDITY_USDC:
                log.info(
                    "[%s] Liquidez insuficiente ($%.0fK < $%.0fK minimo), saltando",
                    market.label,
                    market.liquidity_usdc / 1000,
                    config.MIN_LIQUIDITY_USDC / 1000,
                )
                return

            # ---- Filter 6: momentum (price stable or rising last 6h) ----
            mom_ok, mom_reason = self._check_momentum(market.token_id, price)
            if not mom_ok:
                log.info("[%s] Momentum negativo — %s, BUY bloqueado", market.label, mom_reason)
                return

            # ---- Filter 7: 24h drawdown ----
            dd_ok, dd_reason = self._check_24h_drawdown(market.token_id, price)
            if not dd_ok:
                log.info("[%s] Drawdown excesivo — %s, BUY bloqueado", market.label, dd_reason)
                return

            # ---- Risk check 8: confidence score ----
            confidence, breakdown = self._calculate_confidence(market, price)
            log.info(
                "[%s] Confianza: %d/100 | %s",
                market.label, confidence, breakdown,
            )
            if confidence < config.MIN_CONFIDENCE:
                log.info(
                    "[%s] BUY descartado — confianza %d < minimo %d",
                    market.label, confidence, config.MIN_CONFIDENCE,
                )
                return

            log.info("[%s] COMPRANDO | Confianza: %d/100 | Razon: %s", market.label, confidence, signal_.reason)
            if self.paper_trading and self._paper:
                ok = self._paper.simulate_buy(market.token_id, market.label, price, size)
                if ok:
                    self._last_trade_time[market.token_id] = time.time()
                    self._reporter.send_trade_alert(
                        "BUY", market.label, price, size,
                        self._paper.balance, paper=True, confidence=confidence,
                    )
            else:
                result = place_limit_order(self.client, market.token_id, "BUY", price, size)
                if result:
                    self._last_trade_time[market.token_id] = time.time()
                    self._positions[market.token_id] = Position(
                        token_id=market.token_id,
                        entry_price=price,
                        shares=round(size / price, 2),
                        size_usdc=size,
                        side="BUY",
                    )
                    self._reporter.send_trade_alert(
                        "BUY", market.label, price, size,
                        balance=0, paper=False, confidence=confidence,
                    )

        elif signal_.action == "SELL" and self._has_open_position(market.token_id):
            # Sentiment filter: hold if news is clearly bullish (let winners run)
            if market.news_query and not self._sentiment.should_sell(market.news_query):
                log.info("[%s] SELL retenido — noticias positivas, manteniendo posicion.", market.label)
                return

            confidence, _ = self._calculate_confidence(market, price)
            log.info("[%s] VENDIENDO | Confianza: %d/100 | Razon: %s", market.label, confidence, signal_.reason)
            if self.paper_trading and self._paper:
                pos = self._paper.positions[market.token_id]
                pnl = self._paper.simulate_sell(market.token_id, market.label, price)
                if pnl is not None:
                    if pnl <= -config.TRADE_LOSS_ALERT_USDC:
                        self._reporter.send_alert(
                            f"*ALERTA: Perdida elevada en trade*\n"
                            f"Mercado: `{market.label}`\n"
                            f"Perdida: `{pnl:.2f} USDC` (umbral: -{config.TRADE_LOSS_ALERT_USDC:.0f} USDC)\n"
                            f"Razon: _{signal_.reason}_"
                        )
                    self._reporter.send_trade_alert(
                        "SELL", market.label, price, pos.size_usdc,
                        self._paper.balance, pnl=pnl, paper=True, confidence=confidence,
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
                        balance=0, pnl=None, paper=False, confidence=confidence,
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

            # Send Telegram daily report at 08:00
            if self._reporter.should_report():
                prices = self._get_current_prices()
                starting = (
                    self._paper.starting_balance
                    if self._paper
                    else config.PAPER_TRADING_BALANCE
                )
                self._reporter.send_daily_report(starting, prices)

            # Send Telegram weekly summary every Monday at 08:00
            if self._reporter.should_weekly_report():
                prices = self._get_current_prices()
                starting = (
                    self._paper.starting_balance
                    if self._paper
                    else config.PAPER_TRADING_BALANCE
                )
                self._reporter.send_weekly_report(starting, prices)

            # Hourly backup of paper_trades.json
            if time.time() - self._last_backup >= config.BACKUP_INTERVAL_SECONDS:
                self._do_backup()
                self._last_backup = time.time()

            if self.running:
                log.info("Esperando %d segundos...", config.LOOP_INTERVAL_SECONDS)
                time.sleep(config.LOOP_INTERVAL_SECONDS)

        log.info("Bot detenido correctamente.")


# ---------------------------------------------------------------------------
# Punto de entrada — Mercados activos (actualizado 2026-03-25)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    markets_to_watch = [

        # 1. SPORTS — OKC Thunder wins 2026 NBA Finals (YES, ~38-39 cents)
        #    Near 50/50 — highest liquidity & daily volume. Mean reversion
        #    catches intraday oscillations. window=10 builds fast; std=0.4
        #    fires on small deviations to maximise trade frequency.
        MarketConfig(
            token_id="49500299856831034491021962156746701298730459370557900271970866855042624695770",
            label="OKC Thunder wins NBA Finals",
            strategy="mean_reversion",
            size_usdc=10,
            news_query="Oklahoma City Thunder NBA Finals 2026",
            mr_window=10,
            mr_std_threshold=0.4,
            liquidity_usdc=301_635,
        ),

        # 2. FIFA — England wins 2026 World Cup (YES, ~13 cents)
        #    buy_below set just above last observed price -> immediate entry.
        #    Take-profit / stop-loss in .env handle the exit.
        MarketConfig(
            token_id="115556263888245616435851357148058235707004733438163639091106356867234218207169",
            label="England wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.135,
            sell_above=0.150,
            size_usdc=10,
            news_query="England FIFA World Cup 2026",
            liquidity_usdc=1_342_046,
        ),

        # 3. FIFA — Argentina wins 2026 World Cup (YES, ~10 cents)
        MarketConfig(
            token_id="18812649149814341758733697580460697418474693998558159483117100240528657629879",
            label="Argentina wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.105,
            sell_above=0.118,
            size_usdc=10,
            news_query="Argentina FIFA World Cup 2026",
            liquidity_usdc=1_117_199,
        ),

        # 4. FIFA — Brazil wins 2026 World Cup (YES, ~8-9 cents)
        MarketConfig(
            token_id="27576533317283401577758999384642760405921738493660383550832555714312627457443",
            label="Brazil wins World Cup 2026",
            strategy="value_threshold",
            buy_below=0.090,
            sell_above=0.102,
            size_usdc=10,
            news_query="Brazil FIFA World Cup 2026",
            liquidity_usdc=923_093,
        ),

        # 5. FIFA — France wins 2026 World Cup (YES, ~10.6 cents)
        #    $65K vol/day, $1.3M liquidity. Mean reversion window=10.
        MarketConfig(
            token_id="108233603819467706476318984012158651931658302669301887462181073562758483842092",
            label="France wins World Cup 2026",
            strategy="mean_reversion",
            size_usdc=10,
            news_query="France FIFA World Cup 2026",
            mr_window=10,
            mr_std_threshold=0.4,
            liquidity_usdc=1_337_354,
        ),

        # 6. FIFA — Germany wins 2026 World Cup (YES, ~5.2 cents)
        #    $91K vol/day, $638K liquidity.
        MarketConfig(
            token_id="81739002353269632749850710185641576213562066971072676369728657545679630163887",
            label="Germany wins World Cup 2026",
            strategy="mean_reversion",
            size_usdc=10,
            news_query="Germany FIFA World Cup 2026",
            mr_window=10,
            mr_std_threshold=0.4,
            liquidity_usdc=638_021,
        ),

        # 7. FIFA — Portugal wins 2026 World Cup (YES, ~6.9 cents)
        #    $109K vol/day, $478K liquidity.
        MarketConfig(
            token_id="45415751658241142530386585138386640503488308219341470020075667342738719018629",
            label="Portugal wins World Cup 2026",
            strategy="mean_reversion",
            size_usdc=10,
            news_query="Portugal FIFA World Cup 2026",
            mr_window=10,
            mr_std_threshold=0.4,
            liquidity_usdc=478_009,
        ),

        # 8. NBA — Boston Celtics wins 2026 Finals (YES, ~11.2 cents)
        #    $20.7K vol/day, $153K liquidity.
        MarketConfig(
            token_id="98951343420969493497594761179562691809954416596888138302255086663562042936451",
            label="Boston Celtics wins NBA Finals",
            strategy="mean_reversion",
            size_usdc=10,
            news_query="Boston Celtics NBA Finals 2026",
            mr_window=10,
            mr_std_threshold=0.4,
            liquidity_usdc=153_062,
        ),

        # 9. CRYPTO — Will Bitcoin hit $1M before GTA VI? (YES, ~48.8 cents)
        #    Best crypto proxy: near-50/50, $423K liquidity, $13.7K vol/day.
        MarketConfig(
            token_id="105267568073659068217311993901927962476298440625043565106676088842803600775810",
            label="Bitcoin hits $1M before GTA VI",
            strategy="mean_reversion",
            size_usdc=10,
            news_query="Bitcoin price 2026",
            mr_window=10,
            mr_std_threshold=0.4,
            liquidity_usdc=423_763,
        ),

    ]

    bot = PolymarketBot(markets=markets_to_watch)
    bot.run()
