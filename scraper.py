"""
Pokemon Price Scraper
======================
Tracks two things every night:
  1. CHASE CARDS    — SIR, Hyper Rare, Illustration Rare singles via pokemontcg.io
  2. SEALED PRODUCTS — Booster boxes & ETBs via PriceCharting free API

Outputs:
  data/history.csv        — card prices
  data/sealed_history.csv — sealed product prices

SETUP:  pip install requests pandas
USAGE:  python scraper.py
"""

import csv, json, time, sys
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    import pandas as pd
except ImportError:
    print("Run: pip install requests pandas")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
HISTORY_FILE        = Path("data/history.csv")
SEALED_HISTORY_FILE = Path("data/sealed_history.csv")
SEALED_PRODUCTS_FILE= Path("sealed_products.json")

for f in [HISTORY_FILE, SEALED_HISTORY_FILE]:
    f.parent.mkdir(parents=True, exist_ok=True)

API_BASE     = "https://api.pokemontcg.io/v2"
PC_API_BASE  = "https://www.pricecharting.com/api"

CHASE_RARITIES = ["Special Illustration Rare", "Hyper Rare", "Illustration Rare"]
MIN_RELEASE    = "2022-01-01"
MIN_PRICE      = 10.00

CARD_COLS = [
    "date", "card_id", "name", "set_name", "set_id",
    "rarity", "card_number", "release_date",
    "market_price", "low_price", "mid_price", "high_price", "tcgplayer_url",
]
SEALED_COLS = [
    "date", "set", "type", "name", "market_price",
    "loose_price", "new_price", "pricecharting_id", "source_url",
]

# ── Shared helpers ────────────────────────────────────────────────────────────

def append_row(path: Path, row: dict, cols: list):
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

def already_today(path: Path, id_col: str, id_val: str, today: str) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        df = pd.read_csv(path, dtype=str)
        return not df[(df["date"] == today) & (df[id_col] == id_val)].empty
    except Exception:
        return False

# ── PART 1: Chase cards (pokemontcg.io) ──────────────────────────────────────

def fetch_cards(rarity: str, page: int) -> dict:
    r = requests.get(f"{API_BASE}/cards", params={
        "q":        f'rarity:"{rarity}"',
        "select":   "id,name,set,rarity,number,tcgplayer",
        "pageSize": 250,
        "page":     page,
        "orderBy":  "-set.releaseDate",
    }, timeout=30)
    r.raise_for_status()
    return r.json()

def extract_card_price(card: dict) -> dict | None:
    tcp      = card.get("tcgplayer", {})
    prices   = tcp.get("prices", {})
    pd_data  = prices.get("holofoil") or prices.get("normal") or \
               (list(prices.values())[0] if prices else None)
    if not pd_data:
        return None
    market = pd_data.get("market") or pd_data.get("mid")
    if not market or market < MIN_PRICE:
        return None
    set_info = card.get("set", {})
    if set_info.get("releaseDate", "9999") < MIN_RELEASE:
        return None
    return {
        "card_id":      card.get("id", ""),
        "name":         card.get("name", ""),
        "set_name":     set_info.get("name", ""),
        "set_id":       set_info.get("id", ""),
        "rarity":       card.get("rarity", ""),
        "card_number":  card.get("number", ""),
        "release_date": set_info.get("releaseDate", ""),
        "market_price": round(market, 2),
        "low_price":    round(pd_data.get("low") or 0, 2),
        "mid_price":    round(pd_data.get("mid") or 0, 2),
        "high_price":   round(pd_data.get("high") or 0, 2),
        "tcgplayer_url": tcp.get("url", ""),
    }

def scrape_cards(today: str):
    print(f"\n── CHASE CARDS ─────────────────────────────────────────────")
    all_cards = []

    for rarity in CHASE_RARITIES:
        print(f"\nFetching: {rarity}")
        page = 1
        while True:
            try:
                data    = fetch_cards(rarity, page)
                cards   = data.get("data", [])
                total   = data.get("totalCount", 0)
                fetched = (page - 1) * 250 + len(cards)
                print(f"  Page {page}: {len(cards)} cards (total: {total})")
                for c in cards:
                    row = extract_card_price(c)
                    if row:
                        all_cards.append(row)
                if fetched >= total or not cards:
                    break
                page += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"  Error: {e}")
                break
        time.sleep(0.5)

    # Deduplicate
    seen = {}
    for c in all_cards:
        cid = c["card_id"]
        if cid not in seen or c["market_price"] > seen[cid]["market_price"]:
            seen[cid] = c

    unique = sorted(seen.values(), key=lambda c: c["market_price"], reverse=True)
    print(f"\nTotal unique chase cards ≥${MIN_PRICE}: {len(unique)}")

    tracked = 0
    for card in unique:
        if already_today(HISTORY_FILE, "card_id", card["card_id"], today):
            continue
        append_row(HISTORY_FILE, {"date": today, **card}, CARD_COLS)
        tracked += 1
        print(f"  ✓  ${card['market_price']:>8.2f}  {card['name']:<35} [{card['set_name']}]")

    print(f"\n  Tracked {tracked} cards today")

# ── PART 2: Sealed products (PriceCharting) ───────────────────────────────────

def search_pricecharting(query: str) -> list:
    """Search PriceCharting for a product, return list of matches."""
    try:
        r = requests.get(f"{PC_API_BASE}/products", params={"q": query, "status": "200"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("products", [])
    except Exception as e:
        print(f"    PC error: {e}")
        return []

def best_match(results: list, product_type: str) -> dict | None:
    """Pick the most relevant result for the given product type."""
    type_keywords = {
        "Booster Box": ["booster box", "booster-box"],
        "ETB":         ["elite trainer", "etb", "elite-trainer"],
    }
    keywords = type_keywords.get(product_type, [product_type.lower()])

    for r in results:
        name = (r.get("name") or "").lower()
        console = (r.get("console-name") or "").lower()
        combined = f"{name} {console}"
        if any(kw in combined for kw in keywords):
            return r
    return results[0] if results else None

def parse_pc_price(cents) -> float | None:
    """PriceCharting returns prices in cents as integers."""
    try:
        v = int(cents)
        return round(v / 100, 2) if v > 0 else None
    except (TypeError, ValueError):
        return None

def scrape_sealed(today: str):
    print(f"\n── SEALED PRODUCTS ─────────────────────────────────────────")

    if not SEALED_PRODUCTS_FILE.exists():
        print("  sealed_products.json not found — skipping")
        return

    products = json.loads(SEALED_PRODUCTS_FILE.read_text())
    tracked  = 0

    for product in products:
        set_name  = product["set"]
        prod_type = product["type"]
        search    = product["search"]
        uid       = f"{set_name}|{prod_type}"

        if already_today(SEALED_HISTORY_FILE, "set", set_name, today):
            # More precise: check set+type combo
            try:
                df = pd.read_csv(SEALED_HISTORY_FILE, dtype=str)
                if not df[(df["date"] == today) & (df["set"] == set_name) & (df["type"] == prod_type)].empty:
                    continue
            except Exception:
                pass

        print(f"  Searching: {set_name} {prod_type}")
        results = search_pricecharting(search)
        match   = best_match(results, prod_type)

        if not match:
            print(f"    ✗  No match found")
            time.sleep(0.5)
            continue

        loose  = parse_pc_price(match.get("loose-price"))
        new_p  = parse_pc_price(match.get("new-price"))
        market = loose or new_p

        if not market:
            print(f"    ✗  No price data")
            time.sleep(0.5)
            continue

        pc_id  = match.get("id", "")
        pc_url = f"https://www.pricecharting.com/game/{pc_id}" if pc_id else ""

        row = {
            "date":              today,
            "set":               set_name,
            "type":              prod_type,
            "name":              match.get("name", search),
            "market_price":      market,
            "loose_price":       loose or "",
            "new_price":         new_p or "",
            "pricecharting_id":  pc_id,
            "source_url":        pc_url,
        }
        append_row(SEALED_HISTORY_FILE, row, SEALED_COLS)
        tracked += 1
        print(f"    ✓  ${market:>8.2f}  {match.get('name', '')}  [{prod_type}]")
        time.sleep(0.4)   # polite rate limiting

    print(f"\n  Tracked {tracked} sealed products today")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"  Pokemon Price Scraper  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}")

    scrape_cards(today)
    scrape_sealed(today)

    print(f"\n{'='*60}")
    print(f"  Cards   → {HISTORY_FILE}")
    print(f"  Sealed  → {SEALED_HISTORY_FILE}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
