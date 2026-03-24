# Polymarket Trading Bot

Bot de trading automático para [Polymarket](https://polymarket.com) escrito en Python.

## Estructura

```
polymarket_bot/
├── bot.py            # Bot principal — edita aquí tus mercados
├── client.py         # Wrapper de la API CLOB de Polymarket
├── strategy.py       # Estrategias de trading
├── config.py         # Carga de configuración (.env)
├── logger.py         # Logger con colores
├── find_markets.py   # Herramienta para descubrir mercados
├── requirements.txt
└── .env.example      # Plantilla de variables de entorno
```

## Instalación

```bash
cd polymarket_bot
pip install -r requirements.txt
cp .env.example .env
```

Edita `.env` con tus credenciales.

## Obtener credenciales

1. Ve a [polymarket.com](https://polymarket.com) e inicia sesión
2. Abre **Settings > API Keys** y crea una clave
3. Copia `API Key`, `Secret` y `Passphrase` en tu `.env`
4. La `PRIVATE_KEY` es la clave privada de tu wallet de Polygon

## Uso

### 1. Descubrir mercados

```bash
python find_markets.py                # Mercados activos
python find_markets.py "trump"        # Buscar por palabra clave
```

Copia el `Token ID` del mercado que te interese.

### 2. Configurar el bot

Abre `bot.py` y edita la lista `markets_to_watch`:

```python
MarketConfig(
    token_id="0xABC123...",        # Token ID del mercado
    label="Elecciones 2026",
    strategy="value_threshold",
    buy_below=0.30,                # Compra si precio < 30%
    sell_above=0.70,               # Vende si precio > 70%
    size_usdc=20,                  # 20 USDC por operación
)
```

### 3. Ejecutar

```bash
python bot.py
```

## Estrategias disponibles

| Estrategia | Descripción |
|---|---|
| `value_threshold` | Compra/vende cuando el precio cruza umbrales fijos |
| `mean_reversion` | Opera cuando el precio se desvía mucho de su media histórica |

## Gestión de riesgo

Configurada en `.env`:

| Variable | Descripción | Por defecto |
|---|---|---|
| `MAX_ORDER_SIZE_USDC` | Máximo por orden | 50 USDC |
| `MAX_TOTAL_EXPOSURE_USDC` | Máximo total expuesto | 200 USDC |
| `STOP_LOSS_PCT` | Cierra si pierde este % | 20% |
| `TAKE_PROFIT_PCT` | Cierra si gana este % | 30% |

## Aviso

Este bot opera con dinero real. Úsalo bajo tu propia responsabilidad.
Empieza siempre con cantidades pequeñas para probar tu estrategia.
