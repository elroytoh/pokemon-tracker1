"""
Pokemon Chase Card Price Scraper
=================================
Uses the free pokemontcg.io API — no browser needed.
Fetches all Special Illustration Rare, Hyper Rare, and Illustration Rare
cards from recent sets, pulls their TCGPlayer market prices, and appends
a row to data/history.csv for each card every night.

SETUP:
    pip install requests pandas

USAGE:
    python scraper.py
"""

import csv
import json
import time
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    import pandas as pd
except ImportError:
    print("Run: pip install requests pandas")
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
API_BASE        = "https://api.pokemontcg.io/v2"
HISTORY_FILE    = Path("data/history.csv")
HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

# Chase rarities to track
CHASE_RARITIES  = [
    "Special Illustration Rare",
    "Hyper Rare",
    "Illustration Rare",
]

# Only pull sets released on or after this date (keeps list manageable)
MIN_RELEASE     = "2022/01/01"

# Minimum TCGPlayer market price to include (filters out low-value cards)
MIN_PRICE       = 10.00

HISTORY_COLS    = [
    "date", "card_id", "name", "set_name", "set_id",
    "rarity", "card_number", "release_date",
    "market_price", "low_price", "mid_price", "high_price",
    "tcgplayer_url",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_cards(rarity: str, page: int = 1) -> dict:
    """Fetch one page of cards for a given rarity from pokemontcg.io."""
    params = {
        "q":        f'rarity:"{rarity}" set.releaseDate:[{MIN_RELEASE} TO *]',
        "select":   "id,name,set,rarity,number,tcgplayer",
        "pageSize": 250,
        "page":     page,
        "orderBy":  "-set.releaseDate",
    }
    r = requests.get(f"{API_BASE}/cards", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def extract_price(card: dict) -> dict | None:
    """Pull TCGPlayer price data out of a card object."""
    tcp = card.get("tcgplayer", {})
    if not tcp:
        return None

    prices = tcp.get("prices", {})
    # Try holofoil first, then normal, then whatever's there
    price_data = prices.get("holofoil") or prices.get("normal") or \
                 (list(prices.values())[0] if prices else None)

    if not price_data:
        return None

    market = price_data.get("market") or price_data.get("mid")
    if not market or market < MIN_PRICE:
        return None

    set_info = card.get("set", {})
    return {
        "card_id":      card.get("id", ""),
        "name":         card.get("name", ""),
        "set_name":     set_info.get("name", ""),
        "set_id":       set_info.get("id", ""),
        "rarity":       card.get("rarity", ""),
        "card_number":  card.get("number", ""),
        "release_date": set_info.get("releaseDate", ""),
        "market_price": round(market, 2),
        "low_price":    round(price_data.get("low") or 0, 2),
        "mid_price":    round(price_data.get("mid") or 0, 2),
        "high_price":   round(price_data.get("high") or 0, 2),
        "tcgplayer_url": tcp.get("url", ""),
    }

def already_tracked_today(card_id: str, today: str) -> bool:
    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return False
    try:
        df = pd.read_csv(HISTORY_FILE, dtype=str)
        return not df[(df["date"] == today) & (df["card_id"] == card_id)].empty
    except Exception:
        return False

def append_row(row: dict):
    write_header = not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    today = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"  Pokemon Chase Card Scraper  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Source: pokemontcg.io  ·  Min price: ${MIN_PRICE}")
    print(f"{'='*60}\n")

    all_cards = []

    for rarity in CHASE_RARITIES:
        print(f"Fetching: {rarity}")
        page = 1
        while True:
            try:
                data    = get_cards(rarity, page)
                cards   = data.get("data", [])
                total   = data.get("totalCount", 0)
                fetched = (page - 1) * 250 + len(cards)

                print(f"  Page {page}: {len(cards)} cards  (total: {total})")

                for card in cards:
                    row = extract_price(card)
                    if row:
                        all_cards.append(row)

                if fetched >= total or not cards:
                    break
                page += 1
                time.sleep(0.3)   # be polite to the API

            except Exception as e:
                print(f"  Error on page {page}: {e}")
                break

        print(f"  → {sum(1 for c in all_cards if c['rarity'] == rarity)} chase cards found so far\n")
        time.sleep(0.5)

    # Deduplicate by card_id (keep highest market price in case of duplicates)
    seen = {}
    for card in all_cards:
        cid = card["card_id"]
        if cid not in seen or card["market_price"] > seen[cid]["market_price"]:
            seen[cid] = card

    unique = list(seen.values())
    unique.sort(key=lambda c: c["market_price"], reverse=True)

    print(f"Total unique chase cards (≥${MIN_PRICE}): {len(unique)}")
    print(f"{'─'*60}")

    tracked = 0
    skipped = 0
    for card in unique:
        if already_tracked_today(card["card_id"], today):
            skipped += 1
            continue
        row = {"date": today, **card}
        append_row(row)
        tracked += 1
        print(f"  ✓  ${card['market_price']:>7.2f}  {card['name']:<35} [{card['set_name']}]")

    print(f"\n{'='*60}")
    print(f"  Tracked: {tracked}  |  Skipped (already today): {skipped}")
    print(f"  History → {HISTORY_FILE.resolve()}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
