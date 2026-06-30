"""
Pokemon Chase Card Price Scraper
==================================
Tracks SIR, Hyper Rare, and Illustration Rare singles via pokemontcg.io (TCGPlayer prices).

Output:  data/history.csv

SETUP:   pip install requests pandas
USAGE:   python scraper.py
"""

import csv, time, sys
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    import pandas as pd
except ImportError:
    print("Run: pip install requests pandas")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
HISTORY_FILE   = Path("data/history.csv")
HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

API_BASE       = "https://api.pokemontcg.io/v2"
CHASE_RARITIES = ["Special Illustration Rare", "Hyper Rare", "Illustration Rare"]
MIN_RELEASE    = "2022-01-01"
MIN_PRICE      = 10.00

CARD_COLS = [
    "date", "card_id", "name", "set_name", "set_id",
    "rarity", "card_number", "release_date",
    "market_price", "low_price", "mid_price", "high_price", "tcgplayer_url",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def append_row(path, row):
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CARD_COLS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

def already_today(card_id, today):
    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return False
    try:
        df = pd.read_csv(HISTORY_FILE, dtype=str)
        return not df[(df["date"] == today) & (df["card_id"] == str(card_id))].empty
    except Exception:
        return False

# ── API ───────────────────────────────────────────────────────────────────────

def fetch_cards(rarity, page):
    r = requests.get(f"{API_BASE}/cards", params={
        "q": f'rarity:"{rarity}"',
        "select": "id,name,set,rarity,number,tcgplayer",
        "pageSize": 250, "page": page,
        "orderBy": "-set.releaseDate",
    }, timeout=30)
    r.raise_for_status()
    return r.json()

def extract_card_price(card):
    tcp    = card.get("tcgplayer", {})
    prices = tcp.get("prices", {})
    pd_    = prices.get("holofoil") or prices.get("normal") or \
             (list(prices.values())[0] if prices else None)
    if not pd_:
        return None
    market = pd_.get("market") or pd_.get("mid")
    if not market or market < MIN_PRICE:
        return None
    s = card.get("set", {})
    if s.get("releaseDate", "9999") < MIN_RELEASE:
        return None
    return {
        "card_id":      card.get("id", ""),
        "name":         card.get("name", ""),
        "set_name":     s.get("name", ""),
        "set_id":       s.get("id", ""),
        "rarity":       card.get("rarity", ""),
        "card_number":  card.get("number", ""),
        "release_date": s.get("releaseDate", ""),
        "market_price": round(market, 2),
        "low_price":    round(pd_.get("low") or 0, 2),
        "mid_price":    round(pd_.get("mid") or 0, 2),
        "high_price":   round(pd_.get("high") or 0, 2),
        "tcgplayer_url": tcp.get("url", ""),
    }

# ── Scraper ───────────────────────────────────────────────────────────────────

def scrape_cards(today):
    print("\n── CHASE CARDS ─────────────────────────────────────────────")
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

    # Deduplicate — keep highest price if card appears twice
    seen = {}
    for c in all_cards:
        cid = c["card_id"]
        if cid not in seen or c["market_price"] > seen[cid]["market_price"]:
            seen[cid] = c
    unique = sorted(seen.values(), key=lambda c: c["market_price"], reverse=True)
    print(f"\nTotal unique chase cards ≥${MIN_PRICE}: {len(unique)}")

    tracked = 0
    for card in unique:
        if already_today(card["card_id"], today):
            continue
        append_row(HISTORY_FILE, {"date": today, **card})
        tracked += 1
        print(f"  ✓  ${card['market_price']:>8.2f}  {card['name']:<35} [{card['set_name']}]")
    print(f"\n  Tracked {tracked} cards today")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"  Pokemon Price Scraper  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}")
    scrape_cards(today)
    print(f"\n{'='*60}")
    print(f"  Cards → {HISTORY_FILE}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
