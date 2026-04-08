"""
Carga y valida la configuracion desde variables de entorno.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Paper trading flag ---
PAPER_TRADING         = os.getenv("PAPER_TRADING", "false").lower() == "true"
PAPER_TRADING_BALANCE = float(os.getenv("PAPER_TRADING_BALANCE", "1000"))


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        if PAPER_TRADING:
            return ""
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

CHAIN_ID = 137 if NETWORK == "polygon" else 80001

# --- Gestion de riesgo ---
MAX_ORDER_SIZE_USDC      = float(os.getenv("MAX_ORDER_SIZE_USDC", "50"))
MAX_TOTAL_EXPOSURE_USDC  = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "300"))
STOP_LOSS_PCT            = float(os.getenv("STOP_LOSS_PCT", "20"))
TAKE_PROFIT_PCT          = float(os.getenv("TAKE_PROFIT_PCT", "30"))

# --- Loop ---
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))

# --- Cooldown entre trades del mismo mercado ---
# OPTIMIZADO: 1h en lugar de 4h para capturar mas movimientos de precio
MIN_TRADE_INTERVAL_SECONDS = int(os.getenv("MIN_TRADE_INTERVAL_SECONDS", str(1 * 3600)))

# --- Gestion de riesgo avanzada ---
BALANCE_FLOOR_USDC   = float(os.getenv("BALANCE_FLOOR_USDC",   "850"))  # pausa si equity cae aqui
MAX_POSITION_PCT     = float(os.getenv("MAX_POSITION_PCT",    "8"))     # max % equity por operacion (subido de 5%)
MAX_EXPOSURE_PCT     = float(os.getenv("MAX_EXPOSURE_PCT",     "40"))   # max % equity en posiciones (subido de 30%)
MIN_CONFIDENCE       = int(os.getenv("MIN_CONFIDENCE",         "55"))   # score minimo para operar (bajado de 60)
MIN_LIQUIDITY_USDC   = float(os.getenv("MIN_LIQUIDITY_USDC",   "100000"))  # mercados con al menos $100K liq
MAX_DRAWDOWN_24H_PCT = float(os.getenv("MAX_DRAWDOWN_24H_PCT", "15"))   # bloquea si bajo >15% en 24h

# --- Alertas y backups ---
TRADE_LOSS_ALERT_USDC   = float(os.getenv("TRADE_LOSS_ALERT_USDC",   "5"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("BACKUP_INTERVAL_SECONDS",   "3600"))

# --- Seguridad y proteccion ---
CONSECUTIVE_LOSS_PAUSE  = int(os.getenv("CONSECUTIVE_LOSS_PAUSE",    "3"))
BLACKLIST_LOSS_USDC     = float(os.getenv("BLACKLIST_LOSS_USDC",      "15"))
NO_TRADE_ALERT_HOURS    = int(os.getenv("NO_TRADE_ALERT_HOURS",       "24"))
STATE_SAVE_INTERVAL_SECONDS = int(os.getenv("STATE_SAVE_INTERVAL_SECONDS", "1800"))

# --- Whale Tracker ---
WHALE_WALLETS: list[str] = []

WC_TOKEN_MAP: dict[str, str] = {
    "115556263888245616435851357148058235707004733438163639091106356867234218207169": "England",
    "18812649149814341758733697580460697418474693998558159483117100240528657629879":  "Argentina",
    "27576533317283401577758999384642760405921738493660383550832555714312627457443":  "Brazil",
    "81739002353269632749850710185641576213562066971072676369728657545679630163887":  "Germany",
    "108233603819467706476318984012158651931658302669301887462181073562758483842092": "France",
}

# --- Kelly Position Sizer ---
# OPTIMIZADO: half-Kelly (0.5) en lugar de quarter-Kelly (0.25)
# Con 83% win rate historico, half-Kelly es conservador pero rentable
KELLY_MIN_SIZE_USDC   = float(os.getenv("KELLY_MIN_SIZE_USDC",   "5"))    # minimo por trade
KELLY_MAX_SIZE_USDC   = float(os.getenv("KELLY_MAX_SIZE_USDC",   "40"))   # maximo por trade (subido de 25)
KELLY_DEFAULT_USDC    = float(os.getenv("KELLY_DEFAULT_USDC",    "12"))   # size si no hay historial (subido de 10)
KELLY_FRACTION        = float(os.getenv("KELLY_FRACTION",        "0.5"))  # half-Kelly
KELLY_MIN_HISTORY     = int(os.getenv("KELLY_MIN_HISTORY",       "10"))   # trades antes de usar Kelly

# --- Volume Spike ---
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "2.0"))
