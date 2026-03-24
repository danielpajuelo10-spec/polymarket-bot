"""
Script de utilidad para encontrar mercados y sus token IDs.

Uso:
    python find_markets.py              # Lista los 20 mercados más activos
    python find_markets.py "bitcoin"    # Busca mercados que contengan "bitcoin"
"""

import sys
import json
import requests

GAMMA_API = "https://gamma-api.polymarket.com"


def list_active_markets(query: str = "", limit: int = 20):
    params = {"active": "true", "limit": limit}
    resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
    resp.raise_for_status()
    markets = resp.json()

    if query:
        markets = [m for m in markets if query.lower() in m.get("question", "").lower()]

    print(f"\n{'='*80}")
    print(f"{'MERCADOS ACTIVOS EN POLYMARKET':^80}")
    print(f"{'='*80}\n")

    if not markets:
        print("No se encontraron mercados.")
        return

    for m in markets:
        print(f"Pregunta:  {m.get('question', 'N/A')}")
        print(f"Volumen:   ${float(m.get('volume', 0)):,.0f} USDC")

        # Cada mercado tiene tokens YES y NO
        tokens = m.get("tokens", [])
        for token in tokens:
            outcome = token.get("outcome", "?")
            token_id = token.get("token_id", "?")
            price = token.get("price", "?")
            print(f"  Token {outcome}: {token_id}  |  Precio actual: {price}")

        condition_id = m.get("conditionId", "N/A")
        print(f"Condition ID: {condition_id}")
        print("-" * 80)


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        list_active_markets(query=query)
    except requests.RequestException as e:
        print(f"Error al conectar con Polymarket: {e}")
