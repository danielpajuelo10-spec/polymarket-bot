"""
Carga y valida la configuración desde variables de entorno.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Paper trading flag (leer primero para condicionar validaciones) ---
PAPER_TRADING         = os.getenv("PAPER_TRADING", "false").lower() == "true"
PAPER_TRADING_BALANCE = float(os.getenv("PAPER_TRADING_BALANCE", "1000"))


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        if PAPER_TRADING:
            return ""   # En paper trading las credenciales son opcionales
        raise EnvironmentError(f"Variable de entorno requerida no encontrada: {key}")
    return val


# --- Credenciales API ---
API_KEY        = _require("POLY_API_KEY")
API_SECRET     = _require("POLY_API_SECRET")
API_PASSPHRASE = _require("POLY_API_PASSPHRASE")
PRIVATE_KEY    = _require("PRIVATE_KEY")

# --- Red ---
NETWORK = os.getenv("NETWORK", "polygon")

CLOB_HOST = (
    "https://clob.polymarket.com"
    if NETWORK == "polygon"
    else "https://clob-staging.polymarket.com"
)

CHAIN_ID = 137 if NETWORK == "polygon" else 80001  # Polygon / Mumbai

# --- Gestión de riesgo ---
MAX_ORDER_SIZE_USDC      = float(os.getenv("MAX_ORDER_SIZE_USDC", "50"))
MAX_TOTAL_EXPOSURE_USDC  = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "200"))
STOP_LOSS_PCT            = float(os.getenv("STOP_LOSS_PCT", "20"))
TAKE_PROFIT_PCT          = float(os.getenv("TAKE_PROFIT_PCT", "30"))

# --- Loop ---
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))

# --- Cooldown entre trades del mismo mercado ---
MIN_TRADE_INTERVAL_SECONDS = int(os.getenv("MIN_TRADE_INTERVAL_SECONDS", str(4 * 3600)))

# --- Gestión de riesgo avanzada ---
BALANCE_FLOOR_USDC = float(os.getenv("BALANCE_FLOOR_USDC", "850"))   # pausa si equity cae aquí
MAX_POSITION_PCT   = float(os.getenv("MAX_POSITION_PCT",   "5"))     # max % equity por operación
MAX_EXPOSURE_PCT   = float(os.getenv("MAX_EXPOSURE_PCT",   "30"))    # max % equity en posiciones
MIN_CONFIDENCE     = int(os.getenv("MIN_CONFIDENCE",       "60"))    # score mínimo para operar
