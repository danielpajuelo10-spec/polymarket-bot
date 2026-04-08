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

import json
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
from telegram_commands import TelegramCommandHandler
from pdf_reporter import generate_weekly_pdf, send_pdf_telegram
from sentiment import SentimentAnalyzer
from whale_tracker import WhaleTracker
from signal_enhancer import VolumeSpike, KellySizer, CorrelationFilter
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

        # Per-market safety: consecutive losses and blacklist
        self._consecutive_losses:  dict[str, int]   = {}
        self._paused_markets:      set[str]          = set()
        self._blacklisted_markets: set[str]          = set()
        self._market_total_pnl:    dict[str, float]  = {}

        # Activity tracking
        self._bot_start_time:       float = time.time()
        self._last_trade_timestamp: float = time.time()  # reset on every executed trade
        self._last_no_trade_alert:  float = 0.0

        # Periodic save / backup state
        self._last_backup:     float = 0.0
        self._last_state_save: float = 0.0

        # Self-optimization engine
        self._optimizer = StrategyOptimizer()

        # News sentiment analyser
        self._sentiment = SentimentAnalyzer()

        # Whale tracker (señal de confirmación on-chain)
        self._whale_tracker = WhaleTracker(
            wallets=config.WHALE_WALLETS,
            lookback_hours=4,
            cache_ttl_seconds=300,
        )

        # Volume spike detector
        self._volume_spike = VolumeSpike(
            spike_threshold=config.VOLUME_SPIKE_THRESHOLD,
            cache_ttl_seconds=180,
        )

        # Kelly position sizer (reemplaza el size fijo de $10)
        self._kelly = KellySizer(
            min_size=config.KELLY_MIN_SIZE_USDC,
            max_size=config.KELLY_MAX_SIZE_USDC,
            default_size=config.KELLY_DEFAULT_USDC,
            kelly_fraction=config.KELLY_FRACTION,
            min_history=config.KELLY_MIN_HISTORY,
        )

        # Correlation filter (evita posiciones contradictorias entre mercados WC)
        self._corr_filter = CorrelationFilter(token_map=config.WC_TOKEN_MAP)
        self._corr_filter.add_exclusive_group(list(config.WC_TOKEN_MAP.keys()))

        # Telegram daily reporter
        self._reporter = TelegramReporter()

        # Telegram command handler (/status, /balance, /pause, /resume)
        self._cmd_handler = TelegramCommandHandler(
            self._reporter.token,
            self._reporter.chat_id,
        )
        self._cmd_handler.start(self)

        # Restore persisted safety state from previous run
        self._load_state()

        # Cargar historial de trades en el Kelly sizer para aprovechar datos existentes
        self._load_kelly_history()

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

    # -----------------------------------------------------------------------
    # State persistence (feature: recovery after restart)
    # -----------------------------------------------------------------------

    STATE_FILE = "bot_state.json"

    def _save_state(self) -> None:
        """Persists safety-critical state to bot_state.json every 30 minutes."""
        state = {
            "saved_at":            time.time(),
            "paused_markets":      list(self._paused_markets),
            "blacklisted_markets": list(self._blacklisted_markets),
            "consecutive_losses":  self._consecutive_losses,
            "market_total_pnl":    self._market_total_pnl,
            "last_trade_time":     self._last_trade_time,
            "last_trade_timestamp": self._last_trade_timestamp,
        }
        try:
            with open(self.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            log.info("[STATE] Estado guardado en %s", self.STATE_FILE)
        except Exception as exc:
            log.warning("[STATE] Error al guardar estado: %s", exc)

    def _load_state(self) -> None:
        """Restores safety-critical state from bot_state.json on startup."""
        try:
            with open(self.STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            self._paused_markets       = set(state.get("paused_markets", []))
            self._blacklisted_markets  = set(state.get("blacklisted_markets", []))
            self._consecutive_losses   = state.get("consecutive_losses", {})
            self._market_total_pnl     = state.get("market_total_pnl", {})
            self._last_trade_time      = state.get("last_trade_time", {})
            self._last_trade_timestamp = state.get("last_trade_timestamp", time.time())
            age_min = (time.time() - state.get("saved_at", time.time())) / 60
            log.info(
                "[STATE] Estado restaurado: %d pausados, %d en lista negra (hace %.0f min)",
                len(self._paused_markets), len(self._blacklisted_markets), age_min,
            )
        except FileNotFoundError:
            log.info("[STATE] Sin estado previo — arrancando limpio")
        except Exception as exc:
            log.warning("[STATE] Error al cargar estado: %s", exc)

    def _load_kelly_history(self) -> None:
        """Carga el historial de paper_trades.json en el Kelly sizer."""
        try:
            with open("paper_trades.json", encoding="utf-8") as f:
                data = json.load(f)
            trades = data.get("trades", [])
            self._kelly.load_from_paper_trades(trades)
        except FileNotFoundError:
            log.info("[KELLY] Sin historial previo — usando tamaño por defecto ($%.0f)", config.KELLY_DEFAULT_USDC)
        except Exception as exc:
            log.warning("[KELLY] Error al cargar historial: %s", exc)

    # -----------------------------------------------------------------------
    # Per-market loss tracking (consecutive losses + blacklist)
    # -----------------------------------------------------------------------

    def _update_loss_tracking(self, token_id: str, label: str, pnl: float) -> None:
        """
        Called after every closed position.
        Updates per-market totals, triggers pause on 3 consecutive losses,
        and permanently blacklists a market if total loss exceeds threshold.
        """
        # Accumulate total P&L for this market
        self._market_total_pnl[token_id] = self._market_total_pnl.get(token_id, 0.0) + pnl
        total = self._market_total_pnl[token_id]

        # Mark last trade time
        self._last_trade_timestamp = time.time()

        # --- Blacklist: total loss exceeds threshold ---
        if total <= -config.BLACKLIST_LOSS_USDC and token_id not in self._blacklisted_markets:
            self._blacklisted_markets.add(token_id)
            self._paused_markets.discard(token_id)   # blacklist supersedes pause
            msg = (
                f"*ALERTA: Mercado en lista negra*\n"
                f"Mercado: `{label}`\n"
                f"Perdida acumulada: `{total:.2f} USDC` (limite: -{config.BLACKLIST_LOSS_USDC:.0f} USDC)\n"
                f"_Este mercado no sera operado de nuevo._"
            )
            log.warning("[RISK] %s en lista negra — perdida total %.2f USDC", label, total)
            self._reporter.send_alert(msg)

        # --- Consecutive losses: pause after N in a row ---
        if pnl < 0:
            self._consecutive_losses[token_id] = self._consecutive_losses.get(token_id, 0) + 1
            count = self._consecutive_losses[token_id]
            if count >= config.CONSECUTIVE_LOSS_PAUSE and token_id not in self._paused_markets \
                    and token_id not in self._blacklisted_markets:
                self._paused_markets.add(token_id)
                msg = (
                    f"Mercado pausado: {label} - {count} pérdidas consecutivas"
                )
                log.warning("[RISK] %s", msg)
                self._reporter.send_alert(
                    f"*ALERTA: {msg}*\n"
                    f"Ultima perdida: `{pnl:.2f} USDC`\n"
                    f"Perdida total: `{total:.2f} USDC`"
                )
        else:
            # Win resets the consecutive counter
            self._consecutive_losses[token_id] = 0
            # Also unpause if it was paused (not blacklisted)
            if token_id in self._paused_markets:
                self._paused_markets.discard(token_id)
                log.info("[RISK] %s reactivado tras trade ganador", label)

    # -----------------------------------------------------------------------
    # Health check (internet + Polymarket API + Telegram)
    # -----------------------------------------------------------------------

    def _run_health_check(self) -> dict[str, bool]:
        """Returns a dict of service_name -> ok for the 09:00 health report."""
        import urllib.request as urlreq
        results: dict[str, bool] = {}

        # 1. Internet
        try:
            urlreq.urlopen("https://www.google.com", timeout=5)
            results["Internet"] = True
        except Exception:
            results["Internet"] = False

        # 2. Polymarket API (fetch a midpoint price)
        try:
            price = get_midpoint(self.client, self.markets[0].token_id)
            results["Polymarket API"] = price is not None
        except Exception:
            results["Polymarket API"] = False

        # 3. Telegram Bot API
        try:
            import requests as req
            resp = req.get(
                f"https://api.telegram.org/bot{self._reporter.token}/getMe",
                timeout=5,
            )
            results["Telegram Bot"] = resp.ok
        except Exception:
            results["Telegram Bot"] = False

        return results

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
                        self._update_loss_tracking(market.token_id, market.label, pnl)
                        # Actualizar Kelly con el resultado del trade
                        self._kelly.record_trade(
                            won=(pnl >= 0),
                            pnl_pct=pnl / size_usdc if size_usdc > 0 else 0.0,
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

            # ---- Safety check: blacklisted market ----
            if market.token_id in self._blacklisted_markets:
                log.info("[%s] Mercado en lista negra — saltando permanentemente", market.label)
                return

            # ---- Safety check: market paused (consecutive losses) ----
            if market.token_id in self._paused_markets:
                log.info("[%s] Mercado pausado por perdidas consecutivas — saltando", market.label)
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

            # ---- Filter 9: volume spike (actividad inusual en el mercado) ----
            try:
                spike = self._volume_spike.check(market.token_id)
                if spike.is_spike:
                    log.info(
                        "[%s] Volume spike %s detectado — ratio=%.1fx "
                        "(vol 1h: $%.0f vs baseline: $%.0f/h)",
                        market.label, spike.strength, spike.spike_ratio,
                        spike.recent_volume_usdc, spike.baseline_volume_usdc,
                    )
                    spike_multiplier = 1.2 if spike.spike_ratio < 4.0 else 0.8
                else:
                    spike_multiplier = 1.0
            except Exception as exc:
                log.debug("[%s] VolumeSpike error: %s", market.label, exc)
                spike_multiplier = 1.0

            # ---- Filter 10: whale confirmation ----
            try:
                whale_score = self._whale_tracker.get_confirmation_score(market.token_id, "BUY")
                if whale_score < 0.5 and config.WHALE_WALLETS:
                    log.info(
                        "[%s] Whales van en contra (score=%.2f) — BUY bloqueado",
                        market.label, whale_score,
                    )
                    return
                whale_multiplier = 0.5 + whale_score  # rango 0.5-1.5
                if whale_score > 0.5 and config.WHALE_WALLETS:
                    log.info("[%s] Confirmacion whale (score=%.2f)", market.label, whale_score)
            except Exception as exc:
                log.debug("[%s] WhaleTracker error: %s", market.label, exc)
                whale_multiplier = 1.0

            # ---- Filter 11: correlation check ----
            try:
                open_pos = {}
                if self.paper_trading and self._paper:
                    open_pos = {tid: "BUY" for tid in self._paper.positions}
                else:
                    open_pos = {tid: "BUY" for tid in self._positions}
                corr_warning = self._corr_filter.check_new_trade(market.token_id, "BUY", open_pos)
                if corr_warning:
                    log.warning("[%s] Correlacion: %s", market.label, corr_warning)
                    # No bloquea, pero reduce confianza
                    corr_multiplier = 0.7
                else:
                    corr_multiplier = 1.0
            except Exception as exc:
                log.debug("[%s] CorrelationFilter error: %s", market.label, exc)
                corr_multiplier = 1.0

            # ---- Kelly position sizing (reemplaza size fijo) ----
            confidence_normalized = confidence / 100.0  # 0-1
            combined_multiplier = spike_multiplier * whale_multiplier * corr_multiplier
            kelly_size = self._kelly.get_size(
                balance=self._total_equity(),
                confidence_multiplier=confidence_normalized * combined_multiplier,
            )
            # Respetar los límites de riesgo ya calculados (max_by_pct, max_exposure)
            size = min(kelly_size, max_by_pct)
            size = max(1.0, size)
            log.info(
                "[%s] Kelly size: $%.2f (kelly_raw=$%.2f, conf=%.0f, "
                "spike=%.1fx, whale=%.2f, corr=%.1fx) | stats=%s",
                market.label, size, kelly_size, confidence,
                spike_multiplier, whale_multiplier, corr_multiplier,
                self._kelly.stats,
            )

            log.info("[%s] COMPRANDO | Confianza: %d/100 | Razon: %s", market.label, confidence, signal_.reason)
            if self.paper_trading and self._paper:
                ok = self._paper.simulate_buy(market.token_id, market.label, price, size)
                if ok:
                    self._last_trade_time[market.token_id] = time.time()
                    self._last_trade_timestamp = time.time()
                    self._reporter.send_trade_alert(
                        "BUY", market.label, price, size,
                        self._paper.balance, paper=True, confidence=confidence,
                    )
            else:
                result = place_limit_order(self.client, market.token_id, "BUY", price, size)
                if result:
                    self._last_trade_time[market.token_id] = time.time()
                    self._last_trade_timestamp = time.time()
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
                    # Reset cooldown from SELL time so 4h counts from close, not open
                    self._last_trade_time[market.token_id] = time.time()
                    self._update_loss_tracking(market.token_id, market.label, pnl)
                    # Actualizar Kelly con el resultado del trade
                    self._kelly.record_trade(
                        won=(pnl >= 0),
                        pnl_pct=pnl / pos.size_usdc if pos.size_usdc > 0 else 0.0,
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
                    # Reset cooldown from SELL time so 4h counts from close, not open
                    self._last_trade_time[market.token_id] = time.time()
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
                # Enviar resumen de señales adicionales
                kelly_stats = self._kelly.stats
                whale_status = self._whale_tracker.status()
                if kelly_stats["trades"] > 0:
                    self._reporter.send_alert(
                        f"📊 *Señales adicionales (24h)*\n"
                        f"Kelly — Win rate: `{kelly_stats['win_rate']*100:.1f}%` "
                        f"({kelly_stats['trades']} trades) | "
                        f"Kelly%: `{kelly_stats['kelly_pct']*100:.1f}%`\n"
                        f"Whales monitoreadas: `{whale_status['wallets_monitored']}`\n"
                        f"Caché whale activa: `{whale_status['cache_entries']} tokens`"
                    )

            # Send Telegram weekly summary every Monday at 08:00 + PDF report
            if self._reporter.should_weekly_report():
                prices = self._get_current_prices()
                starting = (
                    self._paper.starting_balance
                    if self._paper
                    else config.PAPER_TRADING_BALANCE
                )
                self._reporter.send_weekly_report(starting, prices)
                # Generate and send PDF report
                pdf_path = generate_weekly_pdf(starting)
                if pdf_path:
                    now_str = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
                    send_pdf_telegram(
                        self._reporter.token,
                        self._reporter.chat_id,
                        pdf_path,
                        caption=f"*Informe semanal PDF — {now_str}*",
                    )

            # Hourly backup of paper_trades.json
            if time.time() - self._last_backup >= config.BACKUP_INTERVAL_SECONDS:
                self._do_backup()
                self._last_backup = time.time()

            # Save complete bot state every 30 minutes (feature 5)
            if time.time() - self._last_state_save >= config.STATE_SAVE_INTERVAL_SECONDS:
                self._save_state()
                self._last_state_save = time.time()

            # Daily health check at 09:00 (feature 4)
            if self._reporter.should_health_check():
                checks = self._run_health_check()
                self._reporter.send_health_check(checks)

            # Alert if bot running > 24h without any trade (feature 3)
            no_trade_threshold = config.NO_TRADE_ALERT_HOURS * 3600
            bot_age = time.time() - self._bot_start_time
            since_trade = time.time() - self._last_trade_timestamp
            since_alert = time.time() - self._last_no_trade_alert
            if (bot_age > no_trade_threshold
                    and since_trade > no_trade_threshold
                    and since_alert > no_trade_threshold):
                self._last_no_trade_alert = time.time()
                self._reporter.send_alert(
                    f"*Bot activo pero sin trades en 24h*\n"
                    f"Ultimo trade: hace `{since_trade / 3600:.1f}h`\n"
                    f"El bot sigue corriendo pero no ha encontrado condiciones de entrada."
                )

            if self.running:
                log.info("Esperando %d segundos...", config.LOOP_INTERVAL_SECONDS)
                time.sleep(config.LOOP_INTERVAL_SECONDS)

        log.info("Bot detenido correctamente.")


# ---------------------------------------------------------------------------
# Punto de entrada — 13 mercados (5 WC fijos + 8 alto volumen)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    markets_to_watch = [
        # --- Mundial 2026 ---
        MarketConfig(token_id="108233603819467706476318984012158651931658302669301887462181073562758483842092", label="France wins World Cup 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="France World Cup 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=2874914),
        MarketConfig(token_id="81739002353269632749850710185641576213562066971072676369728657545679630163887", label="Germany wins World Cup 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Germany World Cup 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=4921217),
        MarketConfig(token_id="115556263888245616435851357148058235707004733438163639091106356867234218207169", label="England wins World Cup 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="England World Cup 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=2708401),
        MarketConfig(token_id="18812649149814341758733697580460697418474693998558159483117100240528657629879", label="Argentina wins World Cup 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Argentina World Cup 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=3544183),
        MarketConfig(token_id="27576533317283401577758999384642760405921738493660383550832555714312627457443", label="Brazil wins World Cup 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Brazil World Cup 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=3097588),
        # --- Alto volumen 2026-04-08 ---
        MarketConfig(token_id="2916184120206223749839849644877707470354946028257066951797428049170871002238", label="US forces enter Iran by April 30?", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="US forces Iran April 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=9334000),
        MarketConfig(token_id="37126434962149084556522721025504254258386171468763869879755961635390358765833", label="Will Beto O'Rourke win 2028 Democratic nomination?", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Beto O'Rourke 2028 nomination", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=1355000),
        MarketConfig(token_id="81633484456710374417893908729547682494682988109908206793820145554076776128889", label="Will Phil Murphy win 2028 Democratic nomination?", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Phil Murphy 2028 nomination", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=2155000),
        MarketConfig(token_id="49500299856831034491021962156746701298730459370557900271970866855042624695770", label="OKC Thunder wins NBA Finals 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="OKC Thunder NBA Finals 2026", mr_window=10, mr_std_threshold=0.4, liquidity_usdc=301635),
        MarketConfig(token_id="98951343420969493497594761179562691809954416596888138302255086663562042936451", label="Boston Celtics wins NBA Finals 2026", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Boston Celtics NBA Finals 2026", mr_window=10, mr_std_threshold=0.4, liquidity_usdc=153062),
        MarketConfig(token_id="31335564527155177318544135513783493075328451393660649396114225549873718295223", label="US forces enter Iran by Dec 31?", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="US forces Iran 2026", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=780000),
        MarketConfig(token_id="22103094389913052942760671847503869600197668843765668268386940527699940774804", label="Will Jasmine Crockett win 2028 Democratic nomination?", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Jasmine Crockett 2028 nomination", mr_window=20, mr_std_threshold=1.5, liquidity_usdc=866000),
        MarketConfig(token_id="10526756807365906821731199390192796247629844406250435651066760888842803600775810", label="Bitcoin hits 1M before GTA VI?", strategy="mean_reversion", buy_below=0.35, sell_above=0.65, size_usdc=10, news_query="Bitcoin price 2026", mr_window=10, mr_std_threshold=0.4, liquidity_usdc=423763),
    ]

    log.info("[STARTUP] %d mercados cargados", len(markets_to_watch))
    bot = PolymarketBot(markets=markets_to_watch)
    bot.run()
