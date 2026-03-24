"""
Wrapper sobre py-clob-client con helpers para el bot.
"""

from __future__ import annotations

import requests
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
)
from py_clob_client.constants import POLYGON

import config
from logger import get_logger

log = get_logger()

# URL de la Gamma API (datos de mercado enriquecidos)
GAMMA_API = "https://gamma-api.polymarket.com"


def build_client() -> ClobClient:
    """
    Crea y devuelve un ClobClient.

    En paper trading mode se conecta sin credenciales (solo lectura de precios).
    En modo real requiere credenciales válidas para firmar órdenes.
    """
    chain_id = POLYGON if config.NETWORK == "polygon" else 80001

    if config.PAPER_TRADING:
        # Sin credenciales: solo se usará para consultar precios públicos
        client = ClobClient(host=config.CLOB_HOST, chain_id=chain_id)
        log.info("Cliente CLOB (solo lectura) conectado a %s [PAPER MODE]", config.CLOB_HOST)
    else:
        creds = ApiCreds(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )
        client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=chain_id,
            key=config.PRIVATE_KEY,
            creds=creds,
        )
        log.info("Cliente CLOB autenticado conectado a %s", config.CLOB_HOST)

    return client


# ---------------------------------------------------------------------------
# Datos de mercado
# ---------------------------------------------------------------------------

def get_markets(limit: int = 50, active_only: bool = True) -> list[dict]:
    """Devuelve mercados desde la Gamma API."""
    params: dict = {"limit": limit}
    if active_only:
        params["active"] = "true"
    resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_market(condition_id: str) -> dict:
    """Devuelve datos de un mercado específico."""
    resp = requests.get(f"{GAMMA_API}/markets/{condition_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_orderbook(client: ClobClient, token_id: str) -> dict:
    """Devuelve el orderbook de un token (YES o NO side de un mercado)."""
    return client.get_order_book(token_id)


def get_midpoint(client: ClobClient, token_id: str) -> Optional[float]:
    """Devuelve el precio medio (midpoint) de un token."""
    try:
        mid = client.get_midpoint(token_id)
        return float(mid["mid"]) if mid else None
    except Exception as exc:
        log.warning("No se pudo obtener midpoint para %s: %s", token_id, exc)
        return None


# ---------------------------------------------------------------------------
# Órdenes
# ---------------------------------------------------------------------------

def place_limit_order(
    client: ClobClient,
    token_id: str,
    side: str,        # "BUY" o "SELL"
    price: float,     # 0.01 – 0.99
    size_usdc: float,
) -> dict | None:
    """
    Coloca una orden límite.

    price    = probabilidad implícita (e.g. 0.65 = 65 cents por acción YES)
    size_usdc = cuántos USDC gastar (en compra) o recibir (en venta)
    """
    if size_usdc > config.MAX_ORDER_SIZE_USDC:
        log.warning(
            "Orden rechazada: tamaño %.2f USDC supera el máximo %.2f USDC",
            size_usdc, config.MAX_ORDER_SIZE_USDC
        )
        return None

    # En Polymarket el 'size' es en número de acciones (contratos)
    shares = round(size_usdc / price, 2)

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=shares,
        side=side.upper(),  # "BUY" o "SELL"
    )

    try:
        resp = client.create_and_post_order(order_args)
        log.info(
            "Orden %s colocada | token=%s | precio=%.3f | acciones=%.2f | USDC=%.2f",
            side.upper(), token_id[:12], price, shares, size_usdc
        )
        return resp
    except Exception as exc:
        log.error("Error al colocar orden: %s", exc)
        return None


def cancel_order(client: ClobClient, order_id: str) -> bool:
    """Cancela una orden abierta."""
    try:
        client.cancel(order_id)
        log.info("Orden %s cancelada", order_id)
        return True
    except Exception as exc:
        log.error("Error al cancelar orden %s: %s", order_id, exc)
        return False


def get_open_orders(client: ClobClient) -> list[dict]:
    """Devuelve todas las órdenes abiertas."""
    try:
        return client.get_orders() or []
    except Exception as exc:
        log.error("Error al obtener órdenes abiertas: %s", exc)
        return []


def get_positions(client: ClobClient) -> list[dict]:
    """Devuelve las posiciones actuales."""
    try:
        return client.get_positions() or []
    except Exception as exc:
        log.error("Error al obtener posiciones: %s", exc)
        return []
