"""
Pokemon Chase Card Price Scraper — Collectr
============================================
Runs nightly via GitHub Actions.

Two modes:
  1. DISCOVER  — browse Collectr's Pokemon explore page, find all cards
                 priced above CHASE_THRESHOLD, save to chase_cards.json
  2. TRACK     — visit every known chase card URL, capture latest price,
                 append a row to data/history.csv

Both modes run every night so new chase cards are picked up automatically.

Local usage:
    pip install playwright pandas && playwright install chromium
    python scraper.py
    python scraper.py --headless false   # watch the browser
    python scraper.py --debug            # print every API call
"""

import argparse, csv, json, os, re, sys
from datetime import date, datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
CHASE_THRESHOLD  = 15.00        # USD — anything above this is a "chase card"
CHASE_CARDS_FILE = Path("chase_cards.json")
HISTORY_FILE     = Path("data/history.csv")
HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

CHASE_RARITIES   = {            # rarity strings to treat as chase-worthy
    "sir", "special illustration rare",
    "hr",  "hyper rare",
    "ir",  "illustration rare",
    "sar", "special art rare",
    "full art", "secret rare",
    "alt art", "rainbow rare",
}

HISTORY_COLS = [
    "date", "product_id", "name", "set_name", "rarity", "card_number",
    "market_price", "low_price", "mid_price", "high_price", "source_url",
]

EXPLORE_URLS = [
    "https://app.getcollectr.com/explore",
    "https://app.getcollectr.com/",
]

# ── CLI ─────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--headless", default="true", choices=["true","false"])
ap.add_argument("--debug",    action="store_true")
ap.add_argument("--timeout",  type=int, default=45)
args = ap.parse_args()

HEADLESS    = args.headless == "true"
TIMEOUT_MS  = args.timeout * 1000

# ── Dependency check ─────────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install playwright pandas && playwright install chromium")
    sys.exit(1)

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_chase_cards() -> list[dict]:
    if CHASE_CARDS_FILE.exists():
        return json.loads(CHASE_CARDS_FILE.read_text())
    return []

def save_chase_cards(cards: list[dict]):
    CHASE_CARDS_FILE.write_text(json.dumps(cards, indent=2))

def is_json(response) -> bool:
    return "json" in response.headers.get("content-type", "")

def is_chase_rarity(rarity: str) -> bool:
    r = rarity.lower().strip()
    return any(cr in r for cr in CHASE_RARITIES)

def extract_price(obj: dict, key: str) -> float | None:
    """Recursively find a price value by key name."""
    if key in obj:
        try:
            return float(str(obj[key]).replace("$","").replace(",",""))
        except (ValueError, TypeError):
            return None
    for v in obj.values():
        if isinstance(v, dict):
            found = extract_price(v, key)
            if found is not None:
                return found
    return None

def find_prices(obj: dict) -> dict:
    """Pull market/low/mid/high prices out of an arbitrary nested dict."""
    price_map = {}
    for label, keys in {
        "market_price": ["marketPrice","market_price","market","price","value","tcgplayer_market"],
        "low_price":    ["lowPrice","low_price","low","minimum"],
        "mid_price":    ["midPrice","mid_price","mid","median","average"],
        "high_price":   ["highPrice","high_price","high","maximum"],
    }.items():
        for k in keys:
            val = extract_price(obj, k)
            if val is not None:
                price_map[label] = val
                break
    return price_map

def find_field(obj: dict, *keys) -> str:
    """Return the first matching key's value as a string."""
    for k in keys:
        if k in obj and obj[k]:
            return str(obj[k])
    # recursive one level
    for v in obj.values():
        if isinstance(v, dict):
            for k in keys:
                if k in v and v[k]:
                    return str(v[k])
    return ""

def product_id_from_url(url: str) -> str:
    m = re.search(r"/product/(\d+)", url)
    return m.group(1) if m else ""

def append_to_history(row: dict):
    today       = date.today().isoformat()
    write_header = not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        row["date"] = today
        w.writerow(row)

def already_tracked_today(product_id: str) -> bool:
    if not HISTORY_FILE.exists():
        return False
    today = date.today().isoformat()
    try:
        df = pd.read_csv(HISTORY_FILE, dtype=str)
        return not df[(df["date"] == today) & (df["product_id"] == str(product_id))].empty
    except Exception:
        return False

# ── Browser session ───────────────────────────────────────────────────────────

class CollectrSession:
    def __init__(self, playwright):
        self.browser  = playwright.chromium.launch(headless=HEADLESS)
        self.context  = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        self.captured: list[dict] = []   # {url, data}
        page = self.context.new_page()
        page.on("response", self._on_response)
        self.page = page

    def _on_response(self, response):
        try:
            if not is_json(response):
                return
            if args.debug:
                print(f"    [API] {response.url}")
            body = response.json()
            self.captured.append({"url": response.url, "data": body})
        except Exception:
            pass

    def goto(self, url: str):
        self.captured.clear()
        try:
            self.page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
        except Exception as e:
            print(f"  warn: {e}")
        # Scroll to trigger lazy-loaded data
        for _ in range(3):
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(1500)
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.wait_for_timeout(3000)

    def click_if_visible(self, *selectors) -> bool:
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    self.page.wait_for_load_state("networkidle", timeout=12000)
                    self.page.wait_for_timeout(3000)
                    return True
            except Exception:
                continue
        return False

    def close(self):
        self.browser.close()

# ── DISCOVER: find chase cards on the explore page ───────────────────────────

def discover_chase_cards(session: CollectrSession) -> list[dict]:
    """
    Visit the Collectr explore/Pokemon pages and collect any product listings
    that look like chase cards (high rarity or price above threshold).
    Returns a list of card dicts to merge into chase_cards.json.
    """
    found = []

    for url in EXPLORE_URLS:
        print(f"\n  → Exploring: {url}")
        session.goto(url)

        # Try to click into Pokemon section
        session.click_if_visible(
            "text=Pokémon", "text=Pokemon",
            "[href*='pokemon']", "button:has-text('Pokemon')",
        )

        # Try rarity / price filters
        for sel in ["text=Special Illustration", "text=Hyper Rare",
                    "text=Illustration Rare", "text=Secret Rare"]:
            session.click_if_visible(sel)

        # Scroll more to load paginated results
        for _ in range(5):
            session.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            session.page.wait_for_timeout(1500)

        # Parse captured API responses
        for item in session.captured:
            data = item["url"]
            body = item["data"]
            records = body if isinstance(body, list) else []
            if isinstance(body, dict):
                for key in ("data","results","items","products","cards"):
                    if key in body and isinstance(body[key], list):
                        records = body[key]
                        break

            for rec in records:
                if not isinstance(rec, dict):
                    continue

                rarity = find_field(rec, "rarity","rarityCode","rarity_type","type")
                name   = find_field(rec, "name","productName","title","cardName")
                set_n  = find_field(rec, "set","setName","expansion","series")
                number = find_field(rec, "number","cardNumber","card_number","num")
                pid    = find_field(rec, "id","productId","product_id","collectrId")
                page_url = find_field(rec, "url","productUrl","slug","link")

                prices = find_prices(rec)
                mkt    = prices.get("market_price", 0) or 0

                chase = is_chase_rarity(rarity) or mkt >= CHASE_THRESHOLD

                if chase and name:
                    found.append({
                        "id":     pid,
                        "name":   name,
                        "set":    set_n,
                        "number": number,
                        "rarity": rarity,
                        "url":    page_url or item["url"],
                    })

    # Deduplicate by id (or name+set if no id)
    seen = set()
    unique = []
    for c in found:
        key = c.get("id") or f"{c['name']}|{c['set']}"
        if key not in seen:
            seen.add(key)
            unique.append(c)

    print(f"\n  Discovered {len(unique)} potential chase card(s)")
    return unique

# ── TRACK: scrape price for a specific product page ──────────────────────────

def scrape_product(session: CollectrSession, card: dict) -> dict | None:
    """
    Visit a specific Collectr product page and capture its price data.
    Returns a history row dict or None if nothing useful was captured.
    """
    url = card.get("url","")
    if not url:
        return None

    pid = card.get("id") or product_id_from_url(url)

    if already_tracked_today(pid):
        print(f"  (already tracked today — skipping {card['name']})")
        return None

    print(f"  → {card['name']} [{card.get('set','')}]  {url}")
    session.goto(url)

    # Try clicking a price history / chart tab
    session.click_if_visible(
        "text=Price History", "text=History", "text=Chart",
        "text=Trends", "button:has-text('History')", "[data-tab='history']",
    )
    session.page.wait_for_timeout(3000)

    # Parse captured JSON for the best price data
    best: dict | None = None
    best_keys = 0

    for item in session.captured:
        body = item["data"]
        candidates = [body] if isinstance(body, dict) else []
        if isinstance(body, dict):
            for key in ("data","product","card","item","result"):
                if key in body and isinstance(body[key], dict):
                    candidates.append(body[key])

        for c in candidates:
            prices = find_prices(c)
            if len(prices) > best_keys:
                best_keys = len(prices)
                best = {
                    "product_id":   pid or find_field(c,"id","productId"),
                    "name":         card.get("name") or find_field(c,"name","productName"),
                    "set_name":     card.get("set")  or find_field(c,"set","setName","expansion"),
                    "rarity":       card.get("rarity") or find_field(c,"rarity","rarityCode"),
                    "card_number":  card.get("number") or find_field(c,"number","cardNumber"),
                    "source_url":   url,
                    **prices,
                }

    if best and best.get("market_price"):
        print(f"    ✓  market ${best['market_price']:.2f}  low ${best.get('low_price',0):.2f}  high ${best.get('high_price',0):.2f}")
        return best

    # Fallback: if we at least know the URL loaded, record a null-price row
    # so we know we attempted it (helps debug)
    print("    ✗  no price data captured")
    return None

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*65}")
    print(f"  Pokemon Chase Card Tracker  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*65}")

    cards = load_chase_cards()
    print(f"\n  Loaded {len(cards)} known chase card(s) from {CHASE_CARDS_FILE}")

    with sync_playwright() as p:
        session = CollectrSession(p)

        # ── 1. Discover new chase cards ──────────────────────────────────
        print("\n── DISCOVER ────────────────────────────────────────────────────")
        new_cards = discover_chase_cards(session)

        # Merge newly discovered cards (avoid duplicates)
        existing_ids = {c.get("id") or f"{c['name']}|{c['set']}" for c in cards}
        added = 0
        for nc in new_cards:
            key = nc.get("id") or f"{nc['name']}|{nc['set']}"
            if key not in existing_ids:
                cards.append(nc)
                existing_ids.add(key)
                added += 1
        if added:
            save_chase_cards(cards)
            print(f"  Added {added} new card(s) to {CHASE_CARDS_FILE}")

        # ── 2. Track prices for all known chase cards ────────────────────
        print("\n── TRACK ───────────────────────────────────────────────────────")
        tracked = 0
        for card in cards:
            row = scrape_product(session, card)
            if row:
                append_to_history(row)
                tracked += 1

        session.close()

    print(f"\n{'='*65}")
    print(f"  Tracked {tracked} / {len(cards)} cards today")
    print(f"  History → {HISTORY_FILE.resolve()}")
    print(f"{'='*65}\n")

if __name__ == "__main__":
    main()
