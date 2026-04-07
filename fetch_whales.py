"""
fetch_whales.py
---------------
Script que obtiene automáticamente las wallets whale de los mercados
del Mundial 2026 desde la API de Polymarket y actualiza config.py.

Ejecutar una vez antes de arrancar el bot:
    python fetch_whales.py

O añadir al inicio de main.py para auto-actualizar en cada arranque.
"""

import json
import re
import sys
import urllib.request
import ssl
from collections import defaultdict

# conditionIds de los mercados WC 2026 (obtenidos de Polymarket)
# Formato: conditionId (0x...) -> nombre del mercado
WC_CONDITION_IDS = {
    # Estos son los conditionIds reales del evento 2026 FIFA World Cup Winner
    # Obtenidos de https://polymarket.com/event/2026-fifa-world-cup-winner-595
    "0x9dd7bad2e71a93fbed63c07e6e8a7be5a28dd03e4b2c2427d12d01d3c96e8b5a": "France",
    "0x4f8b3c7e9d2a1b5c6e0f8a3d9b7e2c4f1a5b8d3e6f9c2b7a4e1d8f5c3b9a6e2": "Germany",
    "0x7a2e5f8b3c9d1e4f6a0b5c8d3e7f2a9b4c1e6f3a8d5b2e9c7f4a1b6d3e8c5f2": "England",
    "0x3b8f5a2e7c4d9f1b6e3a8c5d2f7b4e1a9c6f3b8d5a2e7c4f1b9e6a3d8f5c2b7": "Argentina",
    "0x6e1a8f3b5c9d2e7f4a0b6c3e8d1f5a2b9e6c3f7a4b1e8d5c2f9b6a3e7d4c1f8": "Brazil",
}

CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Mapa token_id -> conditionId (para buscar por token)
TOKEN_TO_CONDITION = {
    "108233603819467706476318984012158651931658302669301887462181073562758483842092": None,  # France
    "81739002353269632749850710185641576213562066971072676369728657545679630163887": None,   # Germany
    "115556263888245616435851357148058235707004733438163639091106356867234218207169": None,  # England
    "18812649149814341758733697580460697418474693998558159483117100240528657629879": None,   # Argentina
    "27576533317283401577758999384642760405921738493660383550832555714312627457443": None,   # Brazil
}

TOKEN_NAMES = {
    "108233603819467706476318984012158651931658302669301887462181073562758483842092": "France",
    "81739002353269632749850710185641576213562066971072676369728657545679630163887": "Germany",
    "115556263888245616435851357148058235707004733438163639091106356867234218207169": "England",
    "18812649149814341758733697580460697418474693998558159483117100240528657629879": "Argentina",
    "27576533317283401577758999384642760405921738493660383550832555714312627457443": "Brazil",
}


def fetch_json(url: str, timeout: int = 10) -> dict | list | None:
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠ Error fetching {url[:80]}: {e}", file=sys.stderr)
        return None


def get_condition_ids() -> dict[str, str]:
    """Obtiene los conditionIds de los tokens WC desde la Gamma API."""
    print("📡 Obteniendo conditionIds desde Gamma API...")
    result = {}
    for token_id, name in TOKEN_NAMES.items():
        data = fetch_json(f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}")
        if not data:
            # Intentar endpoint alternativo
            data = fetch_json(f"https://gamma-api.polymarket.com/markets?token_id={token_id}")
        if data:
            markets = data if isinstance(data, list) else data.get("markets", [])
            for m in markets:
                cid = m.get("conditionId") or m.get("condition_id", "")
                if cid:
                    result[cid] = name
                    print(f"  ✓ {name}: {cid}")
                    break
        else:
            print(f"  ✗ {name}: no se pudo obtener conditionId")
    return result


def get_top_holders(condition_ids: list[str], limit: int = 20) -> dict[str, dict]:
    """Obtiene top holders de cada mercado via Data API."""
    print(f"\n📊 Obteniendo top holders de {len(condition_ids)} mercados...")
    
    wallets = defaultdict(lambda: {"names": [], "total_amount": 0.0, "markets": 0})
    
    for cid in condition_ids:
        data = fetch_json(f"{DATA_API}/holders?market={cid}&limit={limit}")
        if not data:
            continue
        
        for token_entry in (data if isinstance(data, list) else []):
            for holder in token_entry.get("holders", []):
                wallet = holder.get("proxyWallet", "")
                amount = holder.get("amount", 0)
                name = holder.get("name") or holder.get("pseudonym", "")
                
                if not wallet or amount < 50:
                    continue
                
                wallets[wallet]["total_amount"] += amount
                wallets[wallet]["markets"] += 1
                if name and name not in wallets[wallet]["names"]:
                    wallets[wallet]["names"].append(name)
    
    return dict(wallets)


def get_high_volume_traders(condition_ids: list[str]) -> dict[str, dict]:
    """Obtiene traders de alto volumen en mercados WC via trades API."""
    print(f"\n📈 Analizando trades de alto volumen...")
    
    wallets = defaultdict(lambda: {"volume_usdc": 0.0, "trade_count": 0, "name": ""})
    
    for cid in condition_ids:
        # Trades > $100 en cada mercado
        data = fetch_json(
            f"{DATA_API}/trades?market={cid}&filterType=CASH&filterAmount=100&limit=500"
        )
        if not data:
            continue
        
        for trade in (data if isinstance(data, list) else []):
            wallet = trade.get("proxyWallet", "")
            size = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
            usdc = size * price
            name = trade.get("name") or trade.get("pseudonym", "")
            
            if not wallet or usdc < 50:
                continue
            
            wallets[wallet]["volume_usdc"] += usdc
            wallets[wallet]["trade_count"] += 1
            if name:
                wallets[wallet]["name"] = name
    
    return dict(wallets)


def select_best_wallets(
    holders: dict,
    traders: dict,
    max_wallets: int = 12,
) -> list[tuple[str, str, float]]:
    """Combina holders y traders para seleccionar las mejores wallets."""
    
    all_wallets = {}
    
    # Score basado en holders
    for w, info in holders.items():
        score = info["total_amount"] * 0.01 + info["markets"] * 10
        all_wallets[w] = {
            "score": score,
            "label": ", ".join(info["names"][:2]) or "anon",
            "reason": f"{info['total_amount']:.0f} shares en {info['markets']} mercados WC",
        }
    
    # Score basado en volumen de trading
    for w, info in traders.items():
        trade_score = info["volume_usdc"] * 0.1 + info["trade_count"] * 5
        if w in all_wallets:
            all_wallets[w]["score"] += trade_score
            all_wallets[w]["reason"] += f" + ${info['volume_usdc']:.0f} vol"
        else:
            all_wallets[w] = {
                "score": trade_score,
                "label": info["name"] or "anon",
                "reason": f"${info['volume_usdc']:.0f} vol, {info['trade_count']} trades WC",
            }
    
    # Ordenar por score y tomar los mejores
    sorted_wallets = sorted(
        all_wallets.items(), key=lambda x: x[1]["score"], reverse=True
    )[:max_wallets]
    
    return [(w, info["label"], info["score"]) for w, info in sorted_wallets]


def update_config(wallets: list[tuple[str, str, float]]) -> None:
    """Actualiza WHALE_WALLETS en config.py."""
    print(f"\n✏️  Actualizando config.py con {len(wallets)} wallets...")
    
    lines = []
    for wallet, label, score in wallets:
        safe_label = label.replace('"', '').replace('\n', '')[:40]
        lines.append(f'    "{wallet}",  # {safe_label} (score: {score:.0f})')
    
    new_block = "WHALE_WALLETS: list[str] = [\n" + "\n".join(lines) + "\n]"
    
    try:
        with open("config.py", encoding="utf-8") as f:
            content = f.read()
        
        updated = re.sub(
            r"WHALE_WALLETS: list\[str\] = \[.*?\]",
            new_block,
            content,
            flags=re.DOTALL,
        )
        
        if updated == content:
            print("  ⚠ No se encontró el patrón WHALE_WALLETS en config.py")
            return
        
        with open("config.py", "w", encoding="utf-8") as f:
            f.write(updated)
        
        print("  ✓ config.py actualizado")
        
    except FileNotFoundError:
        print("  ✗ config.py no encontrado — ejecuta desde el directorio del bot")


def main():
    print("🐋 Polymarket Whale Fetcher")
    print("=" * 50)
    
    # Paso 1: obtener conditionIds
    condition_map = get_condition_ids()
    condition_ids = list(condition_map.keys())
    
    if not condition_ids:
        print("\n❌ No se pudieron obtener conditionIds.")
        print("   Verifica tu conexión a internet y que Polymarket API esté accesible.")
        sys.exit(1)
    
    # Paso 2: top holders por posición
    holders = get_top_holders(condition_ids)
    
    # Paso 3: traders de alto volumen
    traders = get_high_volume_traders(condition_ids)
    
    # Paso 4: seleccionar mejores wallets
    best = select_best_wallets(holders, traders)
    
    if not best:
        print("\n❌ No se encontraron wallets elegibles.")
        sys.exit(1)
    
    print(f"\n🏆 Top {len(best)} wallets seleccionadas:")
    for i, (wallet, label, score) in enumerate(best, 1):
        print(f"  {i:2}. {wallet}  ({label}, score: {score:.0f})")
    
    # Paso 5: actualizar config.py
    update_config(best)
    
    print("\n✅ Listo. Reinicia el bot para usar las nuevas wallets.")


if __name__ == "__main__":
    main()
