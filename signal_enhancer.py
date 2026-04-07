"""
signal_enhancer.py
------------------
Mejoras de señal para el bot de Polymarket:

1. VolumeSpike     — detecta volumen inusual ANTES del movimiento de precio
2. KellySizer      — tamaño de posición dinámico basado en edge histórico
3. CorrelationFilter — evita abrir posiciones contradictorias en mercados WC correlacionados

Estos módulos se integran en el pipeline de decisión existente del bot
sin reemplazar la lógica de estrategia (value_threshold / mean_reversion).
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_API_BASE = "https://clob.polymarket.com"


# =============================================================================
# 1. VOLUME SPIKE DETECTOR
# =============================================================================

@dataclass
class VolumeSpikeResult:
    token_id: str
    spike_ratio: float          # vol_1h / avg_vol_por_hora_anterior
    recent_volume_usdc: float
    baseline_volume_usdc: float
    is_spike: bool              # True si ratio > spike_threshold
    spike_threshold: float

    @property
    def strength(self) -> str:
        if self.spike_ratio >= 5.0:
            return "EXTREME"
        elif self.spike_ratio >= 3.0:
            return "STRONG"
        elif self.spike_ratio >= 2.0:
            return "MODERATE"
        elif self.is_spike:
            return "WEAK"
        else:
            return "NORMAL"


class VolumeSpike:
    """
    Detecta spikes de volumen inusuales que preceden a movimientos de precio.

    La idea: si el volumen de la última hora es 2x+ el promedio de las
    últimas 4 horas, algo está pasando en ese mercado (noticias, whale activity).

    Esto te da ventana de entrada ANTES de que el precio se mueva.

    Integración en bot.py:
        spike = self.volume_spike.check(token_id)
        if spike.is_spike and spike.spike_ratio > 2.0:
            # Aumentar confianza de la señal existente
            confidence_multiplier = min(2.0, spike.spike_ratio / 2.0)
    """

    def __init__(self, spike_threshold: float = 2.0, cache_ttl_seconds: int = 180):
        self.spike_threshold = spike_threshold
        self.cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, VolumeSpikeResult]] = {}

    def check(self, token_id: str) -> VolumeSpikeResult:
        """Analiza si hay spike de volumen en el token dado."""
        import time
        cached = self._cache.get(token_id)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]

        result = self._fetch_and_analyze(token_id)
        self._cache[token_id] = (time.time(), result)
        return result

    def _fetch_and_analyze(self, token_id: str) -> VolumeSpikeResult:
        try:
            # Obtener trades de las últimas 5 horas
            url = f"{CLOB_API_BASE}/trades"
            params = {"token_id": token_id, "limit": 500}
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()

            data = resp.json()
            items = data.get("data", []) if isinstance(data, dict) else data

            now = datetime.utcnow()
            one_hour_ago = now - timedelta(hours=1)
            five_hours_ago = now - timedelta(hours=5)

            recent_vol = 0.0
            baseline_vol = 0.0

            for item in items:
                try:
                    ts = datetime.utcfromtimestamp(int(item.get("timestamp", 0)))
                    size = float(item.get("size", 0)) * float(item.get("price", 0))

                    if ts >= one_hour_ago:
                        recent_vol += size
                    elif ts >= five_hours_ago:
                        baseline_vol += size
                except Exception:
                    continue

            avg_baseline = baseline_vol / 4 if baseline_vol > 0 else 0.01
            ratio = recent_vol / avg_baseline

            return VolumeSpikeResult(
                token_id=token_id,
                spike_ratio=round(ratio, 2),
                recent_volume_usdc=round(recent_vol, 2),
                baseline_volume_usdc=round(avg_baseline, 2),
                is_spike=ratio >= self.spike_threshold,
                spike_threshold=self.spike_threshold,
            )

        except Exception as e:
            logger.debug(f"VolumeSpike error para {token_id[:16]}...: {e}")
            return VolumeSpikeResult(
                token_id=token_id,
                spike_ratio=1.0,
                recent_volume_usdc=0.0,
                baseline_volume_usdc=0.0,
                is_spike=False,
                spike_threshold=self.spike_threshold,
            )


# =============================================================================
# 2. KELLY POSITION SIZER
# =============================================================================

@dataclass
class TradeRecord:
    """Registro simplificado de un trade para estadísticas de Kelly."""
    won: bool
    pnl_pct: float          # PnL como % del capital arriesgado (e.g. 0.15 = +15%)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class KellySizer:
    """
    Calcula el tamaño óptimo de posición usando el Criterio de Kelly fraccional.

    Kelly completo es agresivo — usamos quarter-Kelly (25%) para ser conservadores.
    Fallback a tamaño fijo si no hay suficiente historial.

    Fórmula Kelly: f* = (b*p - q) / b
    Donde:
        b = ratio ganancia/pérdida promedio
        p = probabilidad de ganar (win rate histórico)
        q = 1 - p

    Integración en bot.py:
        # Al iniciar el bot:
        self.kelly = KellySizer(min_size=5.0, max_size=25.0)

        # Al cargar trades históricos:
        for trade in closed_trades:
            self.kelly.record_trade(trade['pnl'] > 0, trade['pnl'] / trade['size'])

        # Al calcular tamaño de nueva posición:
        size = self.kelly.get_size(balance=self.balance)
    """

    def __init__(
        self,
        min_size: float = 5.0,       # mínimo en USDC
        max_size: float = 25.0,      # máximo en USDC
        default_size: float = 10.0,  # tamaño si no hay historial
        kelly_fraction: float = 0.25,  # quarter-Kelly para seguridad
        min_history: int = 10,       # trades mínimos antes de usar Kelly
        window: int = 50,            # últimos N trades para calcular
    ):
        self.min_size = min_size
        self.max_size = max_size
        self.default_size = default_size
        self.kelly_fraction = kelly_fraction
        self.min_history = min_history
        self.history: deque[TradeRecord] = deque(maxlen=window)

    def record_trade(self, won: bool, pnl_pct: float) -> None:
        """Registra el resultado de un trade completado."""
        self.history.append(TradeRecord(won=won, pnl_pct=abs(pnl_pct)))

    def load_from_paper_trades(self, trades: list[dict]) -> None:
        """
        Carga historial desde el formato de paper_trades.json.
        Llama esto al iniciar el bot para aprovechar el historial existente.
        """
        closed_trades = [t for t in trades if t.get("pnl") is not None]
        for trade in closed_trades:
            pnl = trade["pnl"]
            size = trade["size_usdc"]
            pnl_pct = pnl / size if size > 0 else 0.0
            self.record_trade(won=(pnl >= 0), pnl_pct=pnl_pct)
        logger.info(f"KellySizer: cargados {len(closed_trades)} trades históricos")

    def get_size(self, balance: float, confidence_multiplier: float = 1.0) -> float:
        """
        Calcula el tamaño óptimo de posición en USDC.

        Args:
            balance: balance actual del bot
            confidence_multiplier: 0.5-2.0 según señales adicionales (whale, spike)

        Returns:
            Tamaño en USDC, entre min_size y max_size.
        """
        if len(self.history) < self.min_history:
            # Sin historial suficiente → tamaño fijo ajustado por confianza
            base = self.default_size * confidence_multiplier
            return round(max(self.min_size, min(self.max_size, base)), 2)

        kelly_pct = self._calculate_kelly()

        if kelly_pct <= 0:
            return self.min_size

        # Kelly como % del balance
        kelly_size = balance * kelly_pct * confidence_multiplier

        return round(max(self.min_size, min(self.max_size, kelly_size)), 2)

    def _calculate_kelly(self) -> float:
        """Calcula el Kelly fraccional basado en historial."""
        wins = [t for t in self.history if t.won]
        losses = [t for t in self.history if not t.won]

        if not wins or not losses:
            return 0.0

        p = len(wins) / len(self.history)  # win rate
        q = 1 - p

        avg_win = sum(t.pnl_pct for t in wins) / len(wins)
        avg_loss = sum(t.pnl_pct for t in losses) / len(losses)

        if avg_loss == 0:
            return 0.0

        b = avg_win / avg_loss
        kelly = (b * p - q) / b
        kelly = max(0.0, kelly * self.kelly_fraction)  # quarter-Kelly

        return kelly

    @property
    def stats(self) -> dict:
        """Estadísticas actuales para logs."""
        if not self.history:
            return {"trades": 0, "win_rate": None, "kelly_pct": None}

        wins = sum(1 for t in self.history if t.won)
        win_rate = wins / len(self.history)
        kelly = self._calculate_kelly()

        return {
            "trades": len(self.history),
            "win_rate": round(win_rate, 3),
            "kelly_pct": round(kelly, 4),
        }


# =============================================================================
# 3. CORRELATION FILTER (mercados WC 2026)
# =============================================================================

# Mapa de correlaciones entre tokens del Mundial 2026
# Las probabilidades de todos los ganadores deben sumar ~1.0
# Si suben unos, bajan otros → correlación negativa
WC2026_TOKEN_GROUPS = {
    "world_cup_2026": [
        # token_id -> país (para logging)
        # Si abres LONG en England y LONG en Argentina al mismo tiempo,
        # estás apostando en ambas direcciones del mismo evento
        # El filtro te avisa pero no bloquea — tú decides
    ]
}


class CorrelationFilter:
    """
    Detecta posiciones potencialmente contradictorias en mercados correlacionados.

    En los mercados de "X gana el Mundial 2026", todos los tokens son
    mutuamente excluyentes — solo uno puede ganar. Tener posiciones LONG
    en múltiples países simultáneamente es diversificación válida, pero
    tener señal de SELL en uno y BUY en otro correlacionado merece atención.

    El filtro emite warnings pero no bloquea trades.

    Integración en bot.py:
        self.corr_filter = CorrelationFilter(token_map=WC2026_TOKEN_MAP)
        warning = self.corr_filter.check_new_trade(token_id, "BUY", open_positions)
        if warning:
            logger.warning(f"Correlation warning: {warning}")
    """

    def __init__(self, token_map: Optional[dict[str, str]] = None):
        """
        token_map: {token_id: nombre_legible}
        """
        self.token_map = token_map or {}
        # Grupos de tokens mutuamente excluyentes
        self._groups: list[set[str]] = []

    def add_exclusive_group(self, token_ids: list[str]) -> None:
        """Añade un grupo de tokens mutuamente excluyentes."""
        self._groups.append(set(token_ids))

    def check_new_trade(
        self,
        new_token_id: str,
        new_side: str,
        open_positions: dict[str, str],  # {token_id: "BUY"/"SELL"}
    ) -> Optional[str]:
        """
        Verifica si el nuevo trade crea una posición contradictoria.
        Retorna un mensaje de warning o None si todo está bien.
        """
        for group in self._groups:
            if new_token_id not in group:
                continue

            # Buscar posiciones abiertas en el mismo grupo
            same_group_positions = {
                tid: side for tid, side in open_positions.items()
                if tid in group and tid != new_token_id
            }

            if not same_group_positions:
                continue

            # Detectar contradicción: SELL aquí + BUY en correlacionado
            for existing_tid, existing_side in same_group_positions.items():
                if new_side == "SELL" and existing_side == "BUY":
                    name_new = self.token_map.get(new_token_id, new_token_id[:16] + "...")
                    name_existing = self.token_map.get(existing_tid, existing_tid[:16] + "...")
                    return (
                        f"SELL {name_new} mientras tienes LONG en {name_existing} "
                        f"(misma competición — mercados mutuamente excluyentes)"
                    )

        return None

    def get_exposure_report(self, open_positions: dict[str, str]) -> dict:
        """
        Resumen de exposición correlacionada.
        Útil para el reporte diario de Telegram.
        """
        report = {}
        for i, group in enumerate(self._groups):
            group_positions = {
                tid: side for tid, side in open_positions.items()
                if tid in group
            }
            if group_positions:
                report[f"group_{i}"] = {
                    self.token_map.get(tid, tid[:12]): side
                    for tid, side in group_positions.items()
                }
        return report
