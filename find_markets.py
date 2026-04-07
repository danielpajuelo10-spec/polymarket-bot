"""
find_markets.py
---------------
Descubre automáticamente los mercados con más volumen en Polymarket
y genera la lista de MarketConfig para usar en bot.py.

Uso:
    python find_markets.py              # muestra top 20 mercados por volumen 24h
        python find_markets.py "iran"       # filtra por keyword
            python find_markets.py --update     # actualiza bot.py con los top mercados

            El bot llama a get_top_markets() al arrancar para auto-configurarse.
            """

import json
import re
import sys
import requests

GAMMA_API = "https://gamma-api.polymarket.com"

# Mercados fijos que SIEMPRE vigilar (Mundial 2026)
FIXED_MARKETS = [
        {
                    "token_id": "108233603819467706476318984012158651931658302669301887462181073562758483842092",
                    "label": "France wins World Cup 2026",
                    "news_query": "France World Cup 2026",
                    "liquidity_usdc": 2874914,
        },
        {
                    "token_id": "81739002353269632749850710185641576213562066971072676369728657545679630163887",
                    "label": "Germany wins World Cup 2026",
                    "news_query": "Germany World Cup 2026",
                    "liquidity_usdc": 4921217,
        },
        {
                    "token_id": "115556263888245616435851357148058235707004733438163639091106356867234218207169",
                    "label": "England wins World Cup 2026",
                    "news_query": "England World Cup 2026",
                    "liquidity_usdc": 2708401,
        },
        {
                    "token_id": "18812649149814341758733697580460697418474693998558159483117100240528657629879",
                    "label": "Argentina wins World Cup 2026",
                    "news_query": "Argentina World Cup 2026",
                    "liquidity_usdc": 3544183,
        },
        {
                    "token_id": "27576533317283401577758999384642760405921738493660383550832555714312627457443",
                    "label": "Brazil wins World Cup 2026",
                    "news_query": "Brazil World Cup 2026",
                    "liquidity_usdc": 3097588,
        },
]

# Keywords a excluir (mercados que no funcionan bien con mean reversion)
EXCLUDE_KEYWORDS = [
        "spread", "over/under", "o/u", "moneyline", "total goals",
        "first half", "halftime", "quarter", "game 1", "game 2", "game 3",
        "map 1", "map 2", "map 3", "bo1", "15-minute", "5-minute", "1-minute",
        "btc up or down", "eth up or down", "xrp up or down", "sol up or down",
]


def fetch_top_markets(limit: int = 30, min_liquidity: int = 300_000) -> list[dict]:
        """
            Obtiene los mercados más activos por volumen 24h desde Gamma API.
                Filtra: binarios, liquidez mínima, precio razonable, sin spreads/props.
                    """
        try:
                    resp = requests.get(
                                    f"{GAMMA_API}/markets",
                                    params={
                                                        "active": "true",
                                                        "limit": limit,
                                                        "order": "volume24hr",
                                                        "ascending": "false",
                                                        "liquidityNumMin": min_liquidity,
                                    },
                                    timeout=15,
                    )
                    resp.raise_for_status()
                    markets = resp.json()
                    if isinstance(markets, dict):
                                    markets = markets.get("markets", [])
        except Exception as e:
                    print(f"[find_markets] Error API: {e}", file=sys.stderr)
                    return []

        results = []
        for m in markets:
                    tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                    if isinstance(tokens, str):
                                    try:
                                                        tokens = json.loads(tokens)
except Exception:
                tokens = []

        # Solo mercados binarios (YES/NO con 2 tokens)
        if len(tokens) != 2:
                        continue

        question = (m.get("question") or m.get("title") or "").strip()
        q_lower = question.lower()

        # Excluir props/spreads
        if any(kw in q_lower for kw in EXCLUDE_KEYWORDS):
                        continue

        # Precio razonable (no casi resuelto)
        try:
                        prices = m.get("outcomePrices") or "[]"
                        if isinstance(prices, str):
                                            prices = json.loads(prices)
                                        price = float(prices[0]) if prices else 0.5
except Exception:
            price = 0.5

        if price < 0.04 or price > 0.96:
                        continue

        # Datos del mercado
        vol24 = float(m.get("volume24hr") or m.get("volume24Hour") or 0)
        liquidity = float(m.get("liquidity") or m.get("liquidityNum") or 0)
        slug = m.get("slug") or m.get("market_slug") or ""

        # Inferir news_query desde el título (primeras 5 palabras)
        words = question.split()
        news_query = " ".join(words[:6])

        results.append({
                        "token_id": tokens[0],  # YES token
                        "label": question,
                        "slug": slug,
                        "strategy": "mean_reversion",
                        "buy_below": 0.35,
                        "sell_above": 0.65,
                        "size_usdc": 10,
                        "news_query": news_query,
                        "mr_window": 20,
                        "mr_std_threshold": 1.5,
                        "liquidity_usdc": int(liquidity),
                        "vol24h": int(vol24),
        })

    return results


def get_top_markets(n_dynamic: int = 8) -> list[dict]:
        """
            Devuelve la lista combinada de mercados para el bot:
                - Mercados fijos del Mundial 2026
        - Top N mercados dinámicos por volumen 24h (excluyendo duplicados)

            Llamada desde bot.py al arrancar.
                """
    dynamic = fetch_top_markets(limit=50, min_liquidity=300_000)

    # Excluir tokens que ya están en FIXED
    fixed_tokens = {m["token_id"] for m in FIXED_MARKETS}
    dynamic_filtered = [m for m in dynamic if m["token_id"] not in fixed_tokens]
