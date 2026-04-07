"""
whale_tracker.py
----------------
Módulo de seguimiento de wallets rentables en Polymarket.
Funciona como señal de CONFIRMACIÓN para las señales existentes del bot
(RSS sentiment + mean reversion). No reemplaza la lógica existente,
la refuerza cuando las whales coinciden con tu señal.

Uso en el bot principal:
    from whale_tracker import WhaleTracker
    whale_tracker = WhaleTracker()
    confirmation = whale_tracker.get_confirmation(token_id)
    # confirmation: -1.0 a 1.0 (negativo = whales vendiendo, positivo = comprando)
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wallets conocidas por rentabilidad histórica en mercados de eventos deportivos
# Estas direcciones son públicas on-chain — añade/elimina según tu investigación
# Puedes encontrar más en https://polymarket.com/leaderboard
# ---------------------------------------------------------------------------
DEFAULT_WHALE_WALLETS = [
    # Formato: dirección_polygon -> alias
    # Añade aquí wallets que hayas identificado como rentables
    # Ejemplo (direcciones ficticias — reemplaza con reales del leaderboard):
    # "0xabc123...": "whale_alpha",
    # "0xdef456...": "whale_beta",
]

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"


@dataclass
class WhaleTrade:
    wallet: str
    token_id: str
    side: str          # "BUY" o "SELL"
    price: float
    size: float
    timestamp: datetime


@dataclass
class WhaleSignal:
    token_id: str
    direction: float       # -1.0 (sell pressure) a +1.0 (buy pressure)
    confidence: float      # 0.0 a 1.0
    whale_count: int       # cuántas whales contribuyeron
    volume_usdc: float     # volumen total de whales en las últimas horas
    details: list[WhaleTrade] = field(default_factory=list)

    def is_strong(self, threshold: float = 0.3) -> bool:
        """True si la señal supera el umbral de confianza."""
        return abs(self.direction) >= threshold and self.confidence >= 0.5

    def agrees_with(self, bot_side: str) -> bool:
        """Devuelve True si las whales van en la misma dirección que tu señal."""
        if bot_side.upper() == "BUY":
            return self.direction > 0
        elif bot_side.upper() == "SELL":
            return self.direction < 0
        return False


class WhaleTracker:
    """
    Rastrea actividad de wallets rentables en Polymarket.

    Integración recomendada en bot.py:
        1. En __init__: self.whale_tracker = WhaleTracker(wallets=WHALE_WALLETS)
        2. En _should_trade(): añadir whale confirmation como factor adicional
        3. En _calculate_size(): aumentar size si whale confirma señal

    El tracker NO bloquea trades — actúa como multiplicador de confianza.
    """

    def __init__(
        self,
        wallets: Optional[list[str]] = None,
        lookback_hours: int = 4,
        cache_ttl_seconds: int = 300,  # 5 minutos de caché
    ):
        self.wallets = wallets or DEFAULT_WHALE_WALLETS
        self.lookback_hours = lookback_hours
        self.cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, WhaleSignal]] = {}  # token_id -> (timestamp, signal)
        self._trade_cache: dict[str, list[WhaleTrade]] = defaultdict(list)

        if not self.wallets:
            logger.warning(
                "WhaleTracker iniciado sin wallets configuradas. "
                "Añade direcciones en whale_tracker.py o pásalas al constructor. "
                "Puedes encontrarlas en https://polymarket.com/leaderboard"
            )

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def get_confirmation(self, token_id: str) -> WhaleSignal:
        """
        Devuelve una señal de -1.0 a +1.0 basada en actividad reciente de whales.
        Resultado cacheado 5 min para no saturar la API.
        """
        cached = self._get_cached(token_id)
        if cached:
            return cached

        trades = self._fetch_recent_trades(token_id)
        signal = self._calculate_signal(token_id, trades)
        self._set_cache(token_id, signal)
        return signal

    def get_confirmation_score(self, token_id: str, bot_side: str) -> float:
        """
        Score de 0.0 a 1.0:
        - 1.0 = whales muy alineadas con tu señal
        - 0.5 = sin actividad whale (neutral, no penaliza)
        - 0.0 = whales van en contra de tu señal
        """
        if not self.wallets:
            return 0.5  # neutral si no hay wallets configuradas

        signal = self.get_confirmation(token_id)

        if not signal.is_strong():
            return 0.5  # sin señal clara → neutral

        if signal.agrees_with(bot_side):
            # Cuanto más fuerte la señal, más cerca de 1.0
            return 0.5 + abs(signal.direction) * 0.5
        else:
            # Van en contra → penalizar
            return 0.5 - abs(signal.direction) * 0.5

    def get_volume_spike(self, token_id: str) -> float:
        """
        Detecta spikes de volumen inusuales.
        Retorna ratio: volumen_ultima_hora / promedio_hora_anterior_4h
        Un ratio > 2.0 indica actividad inusual.
        """
        try:
            trades = self._fetch_all_recent_trades(token_id, hours=5)
            if not trades:
                return 1.0

            now = datetime.utcnow()
            one_hour_ago = now - timedelta(hours=1)
            four_hours_ago = now - timedelta(hours=5)

            recent_vol = sum(t.size for t in trades if t.timestamp >= one_hour_ago)
            older_vol = sum(t.size for t in trades if four_hours_ago <= t.timestamp < one_hour_ago)

            if older_vol == 0:
                return 1.0

            # Normalizar: volumen reciente vs promedio por hora de las 4h anteriores
            avg_older = older_vol / 4
            return recent_vol / avg_older if avg_older > 0 else 1.0

        except Exception as e:
            logger.debug(f"Error calculando volume spike para {token_id[:16]}...: {e}")
            return 1.0

    # ------------------------------------------------------------------
    # Lógica interna
    # ------------------------------------------------------------------

    def _fetch_recent_trades(self, token_id: str) -> list[WhaleTrade]:
        """Obtiene trades recientes de las wallets monitoreadas."""
        if not self.wallets:
            return []

        all_trades = []
        cutoff = datetime.utcnow() - timedelta(hours=self.lookback_hours)

        for wallet in self.wallets:
            try:
                trades = self._fetch_wallet_trades(wallet, token_id, cutoff)
                all_trades.extend(trades)
            except Exception as e:
                logger.debug(f"Error fetching trades de {wallet[:10]}...: {e}")
                continue

        return all_trades

    def _fetch_wallet_trades(
        self, wallet: str, token_id: str, cutoff: datetime
    ) -> list[WhaleTrade]:
        """Llama a la API de Polymarket para obtener trades de una wallet específica."""
        try:
            # Gamma API: historial de trades por wallet
            url = f"{GAMMA_API_BASE}/trades"
            params = {
                "maker": wallet,
                "asset_id": token_id,
                "limit": 50,
            }
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            trades = []
            for item in data.get("data", []):
                ts_str = item.get("timestamp") or item.get("created_at", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue

                if ts < cutoff:
                    continue

                side = "BUY" if item.get("side", "").upper() in ("BUY", "YES") else "SELL"
                trades.append(WhaleTrade(
                    wallet=wallet,
                    token_id=token_id,
                    side=side,
                    price=float(item.get("price", 0)),
                    size=float(item.get("size", 0)),
                    timestamp=ts,
                ))

            return trades

        except requests.RequestException as e:
            logger.debug(f"Request error para wallet {wallet[:10]}: {e}")
            return []

    def _fetch_all_recent_trades(self, token_id: str, hours: int = 5) -> list[WhaleTrade]:
        """Obtiene todos los trades del mercado (no solo whales) para calcular volumen."""
        try:
            url = f"{CLOB_API_BASE}/trades"
            params = {"token_id": token_id, "limit": 200}
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            cutoff = datetime.utcnow() - timedelta(hours=hours)
            trades = []
            for item in data.get("data", []) if isinstance(data, dict) else data:
                try:
                    ts = datetime.utcfromtimestamp(int(item.get("timestamp", 0)))
                    if ts < cutoff:
                        continue
                    side = "BUY" if item.get("side", "").upper() == "BUY" else "SELL"
                    trades.append(WhaleTrade(
                        wallet=item.get("maker", ""),
                        token_id=token_id,
                        side=side,
                        price=float(item.get("price", 0)),
                        size=float(item.get("size", 0)),
                        timestamp=ts,
                    ))
                except Exception:
                    continue

            return trades

        except Exception as e:
            logger.debug(f"Error fetching all trades: {e}")
            return []

    def _calculate_signal(self, token_id: str, trades: list[WhaleTrade]) -> WhaleSignal:
        """Calcula señal direccional a partir de los trades de whales."""
        if not trades:
            return WhaleSignal(
                token_id=token_id,
                direction=0.0,
                confidence=0.0,
                whale_count=0,
                volume_usdc=0.0,
            )

        buy_volume = sum(t.size for t in trades if t.side == "BUY")
        sell_volume = sum(t.size for t in trades if t.side == "SELL")
        total_volume = buy_volume + sell_volume

        if total_volume == 0:
            return WhaleSignal(token_id=token_id, direction=0.0, confidence=0.0,
                               whale_count=0, volume_usdc=0.0)

        # Dirección: +1 = todo compras, -1 = todo ventas
        direction = (buy_volume - sell_volume) / total_volume

        # Confianza: más alta con más volumen y más wallets distintas
        unique_whales = len(set(t.wallet for t in trades))
        # Normalizar confianza: crece con volumen (cap en $500) y número de whales
        vol_confidence = min(1.0, total_volume / 500)
        whale_confidence = min(1.0, unique_whales / max(len(self.wallets), 1))
        confidence = (vol_confidence * 0.6 + whale_confidence * 0.4)

        return WhaleSignal(
            token_id=token_id,
            direction=round(direction, 3),
            confidence=round(confidence, 3),
            whale_count=unique_whales,
            volume_usdc=round(total_volume, 2),
            details=trades,
        )

    # ------------------------------------------------------------------
    # Caché
    # ------------------------------------------------------------------

    def _get_cached(self, token_id: str) -> Optional[WhaleSignal]:
        if token_id not in self._cache:
            return None
        cached_at, signal = self._cache[token_id]
        if time.time() - cached_at > self.cache_ttl:
            del self._cache[token_id]
            return None
        return signal

    def _set_cache(self, token_id: str, signal: WhaleSignal) -> None:
        self._cache[token_id] = (time.time(), signal)

    def clear_cache(self) -> None:
        self._cache.clear()

    def status(self) -> dict:
        """Resumen del estado del tracker para logs/Telegram."""
        return {
            "wallets_monitored": len(self.wallets),
            "cache_entries": len(self._cache),
            "lookback_hours": self.lookback_hours,
        }
