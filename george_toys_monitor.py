"""
ASDA George Toys Sale Monitor
Monitors https://direct.asda.com/george/toys-character/D30,default,sc.html?pmid=toys-toy-sale

Uses the Algolia API embedded in the George website — no HTML scraping needed.
All product data (price, stock, EAN, brand) is fetched directly from Algolia.

Alerts on:
  🆕 New listings
  🟢 Back in stock
  📉 Price drops (>=2% AND >£0.05)

Discord embed format compatible with the Keepa FBA Profit Analyser bot.

Env vars:
  DISCORD_WEBHOOK   required
  CHECK_INTERVAL    seconds between checks (default 3600 = 60 min)
  RUN_ONCE          "true" for GitHub Actions

Dependencies: pip install requests
"""

import json
import os
import re
import time
import requests
from datetime import datetime, timezone
from urllib.parse import quote

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Algolia credentials (embedded in George website — public read-only key)
ALGOLIA_APP_ID  = "1KBYJ8SZ65"
ALGOLIA_API_KEY = "fea321f42e897ee5331d030d1ca9c464"
ALGOLIA_INDEX   = "asda_prod__products__default"
ALGOLIA_URL     = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

# Category filter — "Toys & Character" in the Sale & Offers section
CATEGORY_FILTER = 'categoryPageId:"Sale & Offers > Offers > Offers > Toys & Character"'
GEORGE_BASE_URL = "https://direct.asda.com"
LISTING_URL     = f"{GEORGE_BASE_URL}/george/toys-character/D30,default,sc.html?displayAs=George&pmid=toys-toy-sale"

SNAPSHOT_FILE  = "snapshot_george_toys.json"
BASELINE_FLAG  = "baseline_done_george_toys.txt"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))   # 60 min default
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# Alerts thresholds
MIN_DROP_PCT = 0.02   # 2%
MIN_DROP_ABS = 0.05   # £0.05

# Colours
COL_NEW   = 0xE91E8C   # pink
COL_BACK  = 0x9B59B6   # purple
COL_DROP  = 0x00C853   # green

# ---------------------------------------------------------------------------
# ALGOLIA FETCH
# ---------------------------------------------------------------------------

ALGOLIA_HEADERS = {
    "X-Algolia-Application-Id": ALGOLIA_APP_ID,
    "X-Algolia-API-Key": ALGOLIA_API_KEY,
    "Content-Type": "application/json",
}

RETRIEVE_ATTRS = [
    "name", "current_price", "in_stock", "product_id",
    "primary_image", "brand", "objectID", "availability_flag",
]


def fetch_page(page_num):
    """Fetch one page of results from Algolia. Returns (hits, total_hits)."""
    body = {
        "query": "",
        "filters": CATEGORY_FILTER,
        "hitsPerPage": 1000,
        "page": page_num,
        "attributesToRetrieve": RETRIEVE_ATTRS,
        "attributesToHighlight": [],
    }
    try:
        r = requests.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS,
                          json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("hits", []), data.get("nbHits", 0), data.get("nbPages", 1)
    except Exception as e:
        print(f"  [!] Algolia fetch error (page {page_num}): {e}")
        return [], 0, 0


def fetch_all_products():
    """Fetch all toys on sale via Algolia, paginating through all pages."""
    hits, total, total_pages = fetch_page(0)
    if not hits:
        return []

    print(f"  Page 1/{total_pages}: {len(hits)} products (total: {total})")
    all_hits = list(hits)

    for page in range(1, total_pages):
        page_hits, _, _ = fetch_page(page)
        all_hits.extend(page_hits)
        print(f"  Page {page+1}/{total_pages}: +{len(page_hits)} (total: {len(all_hits)})")
        time.sleep(0.5)

    return all_hits


def parse_product(hit):
    """Parse an Algolia hit into a clean product dict."""
    product_id = str(hit.get("product_id") or hit.get("objectID") or "")
    name       = hit.get("name", "")
    price      = hit.get("current_price")
    in_stock   = bool(hit.get("in_stock") or hit.get("availability_flag"))
    brand      = hit.get("brand", "")

    # primary_image is the product EAN/barcode (13-digit UPC/EAN)
    ean_raw = str(hit.get("primary_image") or "").strip()
    ean     = ean_raw if re.match(r"^\d{8,14}$", ean_raw) else ""

    # Image URL — George uses Scene7 CDN with EAN as image ID
    image = ""
    if ean_raw:
        image = f"https://asda.scene7.com/is/image/Asda/{ean_raw}?wid=400&hei=400&fmt=webp"

    # Product URL — construct from product_id
    url = f"{GEORGE_BASE_URL}/george/search?q={product_id}"

    # Stock — both in_stock AND online must be true to be purchasable
    in_stock = bool(hit.get("in_stock")) and bool(hit.get("online", True))

    return {
        "id":       product_id,
        "name":     name,
        "brand":    brand,
        "price":    round(float(price), 2) if price is not None else None,
        "in_stock": in_stock,
        "ean":      ean,
        "image":    image,
        "url":      url,
    }

# ---------------------------------------------------------------------------
# SAS LINKS — uses retail price (inc-VAT) as cost price
# ---------------------------------------------------------------------------

def sas_ean(ean, price):
    if not ean or not price:
        return None
    return (f"https://sas.selleramp.com/sas/lookup/"
            f"?search_term={ean}&sas_cost_price={price:.2f}")


def sas_title(name, price):
    if not price:
        return None
    return (f"https://sas.selleramp.com/sas/lookup/"
            f"?search_term={quote(name)}&sas_cost_price={price:.2f}")

# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def _send(payload):
    if not DISCORD_WEBHOOK:
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            print(f"  [!] Discord rate limited — waiting {wait:.1f}s")
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _core_fields(product):
    ean   = product.get("ean", "")
    brand = product.get("brand", "")
    price = product.get("price")
    name  = product.get("name", "")

    ean_url   = sas_ean(ean, price)
    title_url = sas_title(name, price)

    fields = [
        {"name": "🏷️ Brand",        "value": brand or "-",                         "inline": True},
        {"name": "🔢 GTIN / EAN",    "value": f"`{ean}`" if ean else "-",           "inline": True},
        {"name": "📊 Stock",         "value": "✅ In stock" if product.get("in_stock") else "❌ OOS", "inline": True},
    ]
    if ean_url:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({ean_url})", "inline": True})
    if title_url:
        fields.append({"name": "🔍 SAS Title", "value": f"[Search by title]({title_url})", "inline": True})
    return fields


def _embed(title_text, url, colour, fields, product):
    embed = {
        "title":     title_text,
        "url":       url,
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "George (ASDA) Toys Monitor • direct.asda.com/george"},
    }
    if product.get("image"):
        embed["thumbnail"] = {"url": product["image"]}
    return embed


def notify_new(product):
    price = product.get("price")
    fields = [
        {"name": "💰 New Price",    "value": f"£{price:.2f}" if price else "-", "inline": True},
        {"name": "💷 Was",          "value": "-",                               "inline": True},
    ] + _core_fields(product)

    _send({"embeds": [_embed(
        f"🆕  NEW — {product['name']}",
        product["url"], COL_NEW, fields, product
    )]})
    print(f"  ✅ NEW: {product['name'][:60]}")


def notify_back(product):
    price = product.get("price")
    fields = [
        {"name": "💰 New Price", "value": f"£{price:.2f}" if price else "-", "inline": True},
    ] + _core_fields(product)

    _send({"embeds": [_embed(
        f"🟢  BACK IN STOCK — {product['name']}",
        product["url"], COL_BACK, fields, product
    )]})
    print(f"  ✅ BACK IN STOCK: {product['name'][:55]}")


def notify_drop(product, old_price, new_price, pct):
    drop_str = f"{pct*100:.1f}%"
    abs_drop = old_price - new_price
    fields = [
        {"name": "💰 New Price", "value": f"**£{new_price:.2f}**",             "inline": True},
        {"name": "💰 Was",       "value": f"~~£{old_price:.2f}~~",             "inline": True},
        {"name": "📉 Drop",      "value": f"↓ £{abs_drop:.2f} (-{drop_str})", "inline": True},
    ] + _core_fields(product)

    icon = "🔥" if pct >= 0.20 else ("💰" if pct >= 0.10 else "💵")
    colour = 0x00C853 if pct >= 0.20 else (0x2ECC71 if pct >= 0.10 else 0x82E0AA)

    _send({"embeds": [_embed(
        f"{icon}  PRICE DROP -{drop_str} — {product['name']}",
        product["url"], colour, fields, product
    )]})
    print(f"  ✅ PRICE DROP -{drop_str}: {product['name'][:50]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            bak = f"{SNAPSHOT_FILE}.bak.{int(time.time())}"
            print(f"  [!] Snapshot corrupted — backing up to {bak}")
            try:
                os.rename(SNAPSHOT_FILE, bak)
            except OSError:
                pass
    return {}


def save_snapshot(data):
    tmp = SNAPSHOT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def to_entry(product):
    return {
        "name":     product.get("name", ""),
        "brand":    product.get("brand", ""),
        "url":      product.get("url", ""),
        "ean":      product.get("ean", ""),
        "image":    product.get("image", ""),
        "price":    product.get("price"),
        "in_stock": product.get("in_stock", True),
        "first_seen": product.get("first_seen",
                                  datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# MAIN CHECK
# ---------------------------------------------------------------------------

def run_check():
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now_str}] Checking ASDA George Toys Sale...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    # Fetch all products from Algolia
    raw_products = fetch_all_products()
    if not raw_products:
        print("  [!] No products fetched — skipping")
        return

    products     = [parse_product(h) for h in raw_products]
    current_ids  = {p["id"] for p in products if p["id"]}
    new_ids      = current_ids - known_ids
    gone_ids     = known_ids - current_ids

    print(f"  {len(products)} products | {len(new_ids)} new | {len(gone_ids)} gone")

    if is_first_run:
        print(f"  First run — building baseline. No alerts will fire.")

    alerts_sent  = 0
    new_snapshot = dict(snapshot)

    for product in products:
        pid      = product["id"]
        if not pid:
            continue

        old = snapshot.get(pid, {})

        # Carry forward EAN/image from snapshot if not in this hit
        for key in ("ean", "image"):
            if not product.get(key):
                product[key] = old.get(key, "")

        if is_first_run:
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            new_snapshot[pid] = entry
            continue

        is_new        = pid in new_ids
        was_in_stock  = old.get("in_stock", True)
        now_in_stock  = product.get("in_stock", True)
        old_price     = old.get("price")
        new_price     = product.get("price")

        # NEW product
        if is_new:
            if now_in_stock:
                notify_new(product)
                alerts_sent += 1
                time.sleep(1.5)
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            new_snapshot[pid] = entry
            continue

        # BACK IN STOCK
        if not was_in_stock and now_in_stock:
            notify_back(product)
            alerts_sent += 1
            time.sleep(1.5)

        # PRICE DROP
        elif now_in_stock and old_price and new_price:
            if old_price > 0:
                pct = (old_price - new_price) / old_price
                if pct >= MIN_DROP_PCT and (old_price - new_price) >= MIN_DROP_ABS:
                    notify_drop(product, old_price, new_price, pct)
                    alerts_sent += 1
                    time.sleep(1.5)

        # Update snapshot
        entry = to_entry(product)
        entry["first_seen"] = old.get("first_seen", entry["first_seen"])
        new_snapshot[pid] = entry

    # Mark gone products as OOS in snapshot
    for pid in gone_ids:
        if pid in new_snapshot:
            new_snapshot[pid]["in_stock"] = False
            new_snapshot[pid]["last_updated"] = datetime.now(timezone.utc).isoformat()

    save_snapshot(new_snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline saved — {len(new_snapshot)} products tracked.")
    else:
        print(f"  Done — {alerts_sent} alert(s) | {len(new_snapshot)} products tracked.")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    print("=" * 56)
    print("  ASDA George Toys Sale Monitor")
    print(f"  {LISTING_URL}")
    print(f"  Source: Algolia API (no HTML scraping)")
    print(f"  Category filter: Toys & Character on sale")
    print(f"  Interval: {CHECK_INTERVAL}s ({CHECK_INTERVAL//60} min)")
    print("=" * 56)

    if not DISCORD_WEBHOOK:
        print("  ⚠️  DISCORD_WEBHOOK not set — alerts will be suppressed")

    if RUN_ONCE:
        run_check()
        return

    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [!] Unexpected error: {e}")
        print(f"  Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
