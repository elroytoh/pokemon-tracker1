"""
Pokemon Price Scraper
======================
Tracks two things every night:
  1. CHASE CARDS    — SIR, Hyper Rare, Illustration Rare singles via pokemontcg.io
  2. SEALED PRODUCTS — Booster boxes & ETBs via PokeInsight (HTML scrape)

Outputs:
  data/history.csv        — card prices
  data/sealed_history.csv — sealed product prices

SETUP:  pip install requests pandas beautifulsoup4
USAGE:  python scraper.py
"""

import csv, re, time, sys
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    import pandas as pd
    from bs4 import BeautifulSoup
except ImportError:
    print("Run: pip install requests pandas beautifulsoup4")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
HISTORY_FILE        = Path("data/history.csv")
SEALED_HISTORY_FILE = Path("data/sealed_history.csv")
for f in [HISTORY_FILE, SEALED_HISTORY_FILE]:
    f.parent.mkdir(parents=True, exist_ok=True)

API_BASE        = "https://api.pokemontcg.io/v2"
POKEINSIGHT_URL = "https://www.pokeinsight.com/sealed-products/type"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PokemonPriceTracker/1.0)"}

CHASE_RARITIES = ["Special Illustration Rare", "Hyper Rare", "Illustration Rare"]
MIN_RELEASE    = "2022-01-01"
MIN_PRICE      = 10.00

SEALED_TYPES = [
    ("booster-box", "Booster Box"),
    ("elite-trainer-box", "ETB"),
]

CARD_COLS = [
    "date", "card_id", "name", "set_name", "set_id",
    "rarity", "card_number", "release_date",
    "market_price", "low_price", "mid_price", "high_price", "tcgplayer_url",
]
SEALED_COLS = [
    "date", "set", "type", "name",
    "market_price", "low_price", "high_price", "source_url",
]

# ── Shared helpers ────────────────────────────────────────────────────────────

def append_row(path, row, cols):
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

def already_today(path, id_col, id_val, today):
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        df = pd.read_csv(path, dtype=str)
        return not df[(df["date"] == today) & (df[id_col] == str(id_val))].empty
    except Exception:
        return False

# ── PART 1: Chase cards (pokemontcg.io) ──────────────────────────────────────

def fetch_cards(rarity, page):
    r = requests.get(f"{API_BASE}/cards", params={
        "q": f'rarity:"{rarity}"', "select": "id,name,set,rarity,number,tcgplayer",
        "pageSize": 250, "page": page, "orderBy": "-set.releaseDate",
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
        "card_id": card.get("id",""), "name": card.get("name",""),
        "set_name": s.get("name",""), "set_id": s.get("id",""),
        "rarity": card.get("rarity",""), "card_number": card.get("number",""),
        "release_date": s.get("releaseDate",""),
        "market_price": round(market, 2),
        "low_price":    round(pd_.get("low") or 0, 2),
        "mid_price":    round(pd_.get("mid") or 0, 2),
        "high_price":   round(pd_.get("high") or 0, 2),
        "tcgplayer_url": tcp.get("url",""),
    }

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

# ── PART 2: Sealed products (PokeInsight) ────────────────────────────────────

def clean_name(raw):
    """Remove duplicated name text that PokeInsight repeats in the link."""
    raw = raw.strip()
    words = raw.split()
    half  = len(words) // 2
    if half > 0 and words[:half] == words[half:]:
        return " ".join(words[:half])
    return raw

def fetch_pokeinsight(type_slug):
    """
    Scrape PokeInsight's sealed product listing page.
    Returns list of {name, market_price, low_price, high_price, url, slug}
    """
    url = f"{POKEINSIGHT_URL}/{type_slug}"
    r   = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    soup     = BeautifulSoup(r.text, "html.parser")
    products = []

    # Each product is an <a> link pointing to /sealed-products/{slug}
    for link in soup.find_all("a", href=re.compile(r"^https?://www\.pokeinsight\.com/sealed-products/[^/]+$")):
        href = link["href"]
        text = link.get_text(" ", strip=True)

        # Market price: first dollar amount in the text
        market_match = re.search(r"\$([\d,]+\.?\d*)", text)
        if not market_match:
            continue
        market = float(market_match.group(1).replace(",", ""))
        if market <= 0:
            continue

        # Price range (low - high)
        range_matches = re.findall(r"\$([\d,]+\.?\d*)", text)
        low  = float(range_matches[1].replace(",","")) if len(range_matches) > 1 else None
        high = float(range_matches[2].replace(",","")) if len(range_matches) > 2 else None

        # Name: text before the first $
        name_part = text.split("$")[0]
        name = clean_name(name_part)

        products.append({
            "name":         name,
            "market_price": market,
            "low_price":    low,
            "high_price":   high,
            "url":          href,
            "slug":         href.rstrip("/").split("/")[-1],
        })

    return products

def set_name_from_slug(slug, prod_type):
    """Convert 'surging-sparks-booster-box' → 'Surging Sparks'"""
    suffix = "-booster-box" if "booster" in slug else \
             "-elite-trainer-box" if "elite" in slug else \
             "-" + slug.split("-")[-1]
    base = slug.replace(suffix, "").replace("-enhanced","").replace("-half","")
    return base.replace("-", " ").title()

def scrape_sealed(today):
    print("\n── SEALED PRODUCTS (PokeInsight) ───────────────────────────")

    for type_slug, type_label in SEALED_TYPES:
        print(f"\nFetching: {type_label}s")
        try:
            products = fetch_pokeinsight(type_slug)
            print(f"  Found {len(products)} products")
        except Exception as e:
            print(f"  Error: {e}")
            continue

        tracked = 0
        for p in products:
            uid = p["slug"]
            if already_today(SEALED_HISTORY_FILE, "source_url", p["url"], today):
                continue

            set_name = set_name_from_slug(p["slug"], type_label)
            row = {
                "date":         today,
                "set":          set_name,
                "type":         type_label,
                "name":         p["name"],
                "market_price": p["market_price"],
                "low_price":    p["low_price"] or "",
                "high_price":   p["high_price"] or "",
                "source_url":   p["url"],
            }
            append_row(SEALED_HISTORY_FILE, row, SEALED_COLS)
            tracked += 1
            print(f"  ✓  ${p['market_price']:>8.2f}  {p['name']}")

        print(f"  Tracked {tracked} {type_label}s today")
        time.sleep(1)

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
