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
BALANCE_FLOOR_USDC   = float(os.getenv("BALANCE_FLOOR_USDC",   "850"))  # pausa si equity cae aquí
MAX_POSITION_PCT     = float(os.getenv("MAX_POSITION_PCT",     "5"))    # max % equity por operación
MAX_EXPOSURE_PCT     = float(os.getenv("MAX_EXPOSURE_PCT",     "30"))   # max % equity en posiciones
MIN_CONFIDENCE       = int(os.getenv("MIN_CONFIDENCE",         "60"))   # score mínimo para operar
MIN_LIQUIDITY_USDC   = float(os.getenv("MIN_LIQUIDITY_USDC",   "150000"))  # omite mercados ilíquidos
MAX_DRAWDOWN_24H_PCT = float(os.getenv("MAX_DRAWDOWN_24H_PCT", "15"))   # bloquea si cayó >15% en 24h

# --- Alertas y backups ---
TRADE_LOSS_ALERT_USDC   = float(os.getenv("TRADE_LOSS_ALERT_USDC",   "5"))     # alerta si trade pierde > X USDC
BACKUP_INTERVAL_SECONDS = int(os.getenv("BACKUP_INTERVAL_SECONDS",   "3600"))  # backup cada hora

# --- Seguridad y protección ---
CONSECUTIVE_LOSS_PAUSE  = int(os.getenv("CONSECUTIVE_LOSS_PAUSE",    "3"))     # pausar mercado tras N pérdidas seguidas
BLACKLIST_LOSS_USDC     = float(os.getenv("BLACKLIST_LOSS_USDC",     "15"))    # lista negra si pérdida total > X USDC
NO_TRADE_ALERT_HOURS    = int(os.getenv("NO_TRADE_ALERT_HOURS",      "24"))    # alerta si sin trades durante N horas
STATE_SAVE_INTERVAL_SECONDS = int(os.getenv("STATE_SAVE_INTERVAL_SECONDS", "1800"))  # guardar estado cada 30 min

# --- Whale Tracker ---
# Añade direcciones Polygon de wallets rentables del leaderboard de Polymarket:
# https://polymarket.com/leaderboard  (filtra por Sports / World Cup 2026)
WHALE_WALLETS: list[str] = [
    # "0xABC123...",  # ejemplo — reemplaza con wallets reales
]

# Mapa token_id -> nombre legible (para logs del whale tracker)
WC_TOKEN_MAP: dict[str, str] = {
    "115556263888245616435851357148058235707004733438163639091106356867234218207169": "England",
    "18812649149814341758733697580460697418474693998558159483117100240528657629879":  "Argentina",
    "27576533317283401577758999384642760405921738493660383550832555714312627457443":  "Brazil",
    "81739002353269632749850710185641576213562066971072676369728657545679630163887":  "Germany",
    "108233603819467706476318984012158651931658302669301887462181073562758483842092": "France",
}

# --- Kelly Position Sizer ---
KELLY_MIN_SIZE_USDC   = float(os.getenv("KELLY_MIN_SIZE_USDC",   "5"))    # tamaño mínimo por trade
KELLY_MAX_SIZE_USDC   = float(os.getenv("KELLY_MAX_SIZE_USDC",   "25"))   # tamaño máximo por trade
KELLY_DEFAULT_USDC    = float(os.getenv("KELLY_DEFAULT_USDC",    "10"))   # size si no hay historial
KELLY_FRACTION        = float(os.getenv("KELLY_FRACTION",        "0.25")) # quarter-Kelly (conservador)
KELLY_MIN_HISTORY     = int(os.getenv("KELLY_MIN_HISTORY",       "10"))   # trades antes de usar Kelly

# --- Volume Spike ---
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "2.0"))  # ratio para considerar spike
