"""
Estrategias de trading para Polymarket.

Cada estrategia recibe el estado del mercado y devuelve una señal:
  {"action": "BUY"|"SELL"|"HOLD", "price": float, "size_usdc": float}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config
from logger import get_logger

log = get_logger()


@dataclass
class Signal:
    action: str          # "BUY", "SELL", "HOLD"
    token_id: str
    price: float
    size_usdc: float
    reason: str = ""


# ---------------------------------------------------------------------------
# Estrategia 1: Value Threshold
# Compra si el precio está por debajo de un umbral (mercado infravalorado)
# Vende si el precio supera el objetivo de ganancia
# ---------------------------------------------------------------------------

def value_threshold_strategy(
    token_id: str,
    current_price: float,
    buy_below: float = 0.35,       # Compra si precio < 35 cents
    sell_above: float = 0.65,      # Vende si precio > 65 cents
    size_usdc: float = None,
) -> Signal:
    """
    Estrategia simple de valor:
    - Compra cuando el mercado parece infravalorarlo (precio bajo)
    - Vende cuando el precio se acerca a lo que crees que vale

    Parámetros:
        buy_below:  Compra si el precio es menor que este valor
        sell_above: Vende si el precio es mayor que este valor
    """
    size = size_usdc or config.MAX_ORDER_SIZE_USDC

    if current_price < buy_below:
        return Signal(
            action="BUY",
            token_id=token_id,
            price=current_price,
            size_usdc=size,
            reason=f"Precio {current_price:.3f} < umbral compra {buy_below:.3f}",
        )
    elif current_price > sell_above:
        return Signal(
            action="SELL",
            token_id=token_id,
            price=current_price,
            size_usdc=size,
            reason=f"Precio {current_price:.3f} > umbral venta {sell_above:.3f}",
        )
    else:
        return Signal(
            action="HOLD",
            token_id=token_id,
            price=current_price,
            size_usdc=0,
            reason=f"Precio {current_price:.3f} dentro del rango neutro",
        )


# ---------------------------------------------------------------------------
# Estrategia 2: Mean Reversion
# Asume que precios extremos revierten a la media histórica
# ---------------------------------------------------------------------------

class MeanReversionStrategy:
    """
    Mantiene una ventana de precios históricos.
    Compra cuando el precio cae X desviaciones estándar por debajo de la media.
    Vende cuando supera la media.
    """

    def __init__(self, window: int = 20, std_threshold: float = 1.5):
        self.window = window
        self.std_threshold = std_threshold
        self._prices: list[float] = []

    def update(self, price: float):
        self._prices.append(price)
        if len(self._prices) > self.window:
            self._prices.pop(0)

    def evaluate(self, token_id: str, current_price: float) -> Signal:
        if len(self._prices) < self.window:
            return Signal("HOLD", token_id, current_price, 0, "Acumulando datos históricos")

        mean = sum(self._prices) / len(self._prices)
        variance = sum((p - mean) ** 2 for p in self._prices) / len(self._prices)
        std = variance ** 0.5

        if std == 0:
            return Signal("HOLD", token_id, current_price, 0, "Sin volatilidad")

        z_score = (current_price - mean) / std

        if z_score < -self.std_threshold:
            return Signal(
                action="BUY",
                token_id=token_id,
                price=current_price,
                size_usdc=config.MAX_ORDER_SIZE_USDC,
                reason=f"Z-score={z_score:.2f} (precio muy bajo respecto a media {mean:.3f})",
            )
        elif z_score > self.std_threshold:
            return Signal(
                action="SELL",
                token_id=token_id,
                price=current_price,
                size_usdc=config.MAX_ORDER_SIZE_USDC,
                reason=f"Z-score={z_score:.2f} (precio muy alto respecto a media {mean:.3f})",
            )
        else:
            return Signal("HOLD", token_id, current_price, 0, f"Z-score={z_score:.2f} neutral")


# ---------------------------------------------------------------------------
# Gestión de riesgo: Stop-Loss / Take-Profit
# ---------------------------------------------------------------------------

def check_exit_conditions(
    token_id: str,
    entry_price: float,
    current_price: float,
    size_usdc: float,
    side: str = "BUY",
) -> Signal:
    """
    Evalúa si una posición abierta debe cerrarse por SL o TP.

    Para posiciones LONG (compramos YES):
      - Stop-loss si el precio baja X% desde la entrada
      - Take-profit si el precio sube X% desde la entrada
    """
    if entry_price <= 0:
        return Signal("HOLD", token_id, current_price, 0, "Sin precio de entrada")

    change_pct = ((current_price - entry_price) / entry_price) * 100

    if side.upper() == "BUY":
        if change_pct <= -config.STOP_LOSS_PCT:
            return Signal(
                action="SELL",
                token_id=token_id,
                price=current_price,
                size_usdc=size_usdc,
                reason=f"STOP-LOSS activado: -{abs(change_pct):.1f}% (entrada={entry_price:.3f})",
            )
        elif change_pct >= config.TAKE_PROFIT_PCT:
            return Signal(
                action="SELL",
                token_id=token_id,
                price=current_price,
                size_usdc=size_usdc,
                reason=f"TAKE-PROFIT activado: +{change_pct:.1f}% (entrada={entry_price:.3f})",
            )

    return Signal("HOLD", token_id, current_price, 0, f"Cambio={change_pct:+.1f}%")
