"""Awin: products, earnings and a join list, alongside or instead of CJ.

Needs AWIN_API_TOKEN (publisher API token from Awin's UI), AWIN_PUBLISHER_ID, and AWIN_FEED_KEY
(the product-feed API key; it's the hex after /apikey/ in any Create-a-Feed download URL).

Endpoints and fields follow working open-source integrations (DealRadar's feed and programme sync,
whitelabeled/awin-api-client's transactions) plus Awin's programmedetails reference:
  - programmes / programmedetails (REST, JSON): joined programmes and their commission range.
  - feedList + per-feed CSV downloads: products with aw_deep_link, the tracking link.
  - transactions: earnings, matched to products through the link's clickref.
Awin's own EPC isn't used for bids: its unit (per click or per 100 clicks) isn't confirmed.
"""
import csv
import gzip
import io
import os
import re
from datetime import date, timedelta

from . import http

API = "https://api.awin.com"
FEED_LIST = "https://ui.awin.com/productdata-darwin-download/publisher/{pub}/{key}/1/feedList"
FEED = ("https://productdata.awin.com/datafeed/download/apikey/{key}/fid/{fid}/columns/{columns}"
        "/format/csv/delimiter/%2C/compression/gzip/")
COLUMNS = "aw_deep_link,product_name,aw_product_id,search_price,currency,merchant_id,merchant_name,aw_image_url,in_stock"
PRODUCTS_PER_PROGRAMME = 25
CANDIDATES = 40
DETAIL_LOOKUPS = 15  # programmedetails calls per run; Awin allows 20 API calls a minute


def configured() -> bool:
    return all(os.getenv(k) for k in ("AWIN_API_TOKEN", "AWIN_PUBLISHER_ID", "AWIN_FEED_KEY"))


def _get(path: str, **params):
    return http.request("GET", f"{API}/publishers/{os.environ['AWIN_PUBLISHER_ID']}{path}", timeout=60,
                        headers={"Authorization": f"Bearer {os.environ['AWIN_API_TOKEN']}"},
                        params=params).json()


def programmes(relationship: str) -> list[dict]:
    """relationship: "joined" or "notjoined"."""
    return _get("/programmes", relationship=relationship) or []


def commission_rate(advertiser_id, relationship: str = "joined") -> float | None:
    """Highest percentage commission across the programme's commission groups, as a fraction."""
    details = _get("/programmedetails", advertiserId=advertiser_id, relationship=relationship) or {}
    rates = [float(c["max"]) / 100 for c in details.get("commissionRange") or []
             if str(c.get("type", "")).lower().startswith("percent") and c.get("max") is not None]
    return max(rates) if rates else None


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def discover(niche: str, out_path: str) -> str:
    """Write a products CSV from joined Awin programmes' feeds, keeping products whose name shares
    a word with the niche (all products when no word matches anything)."""
    rates = {}
    for p in programmes("joined")[:DETAIL_LOOKUPS]:
        rate = commission_rate(p["id"])
        if rate:
            rates[str(p["id"])] = rate
    feeds = list(csv.DictReader(io.StringIO(http.request(
        "GET", FEED_LIST.format(pub=os.environ["AWIN_PUBLISHER_ID"], key=os.environ["AWIN_FEED_KEY"]),
        timeout=60).text)))
    words = _words(niche)
    rows = []
    for f in feeds:
        adv = str(f.get("Advertiser ID", ""))
        if f.get("Membership Status") != "active" or f.get("Datafeed Format") == "Google" or adv not in rates:
            continue
        for item in _feed_products(f["Feed ID"].lstrip("Ff"), words):
            pid = "aw" + re.sub(r"[^A-Za-z0-9]+", "", item["aw_product_id"])[:30]
            link = item["aw_deep_link"].strip()
            rows.append({
                "id": pid, "name": item["product_name"][:100],
                "url": f"{link}{'&' if '?' in link else '?'}clickref={pid}",  # ties earnings to this product
                "price": float(item["search_price"]), "commission_rate": rates[adv],
                "program": item.get("merchant_name", ""), "notes": "found automatically on Awin",
                "epc": "", "image_url": item.get("aw_image_url", "").strip(),
            })
        if len(rows) >= CANDIDATES:
            break
    if not rows:
        raise ValueError("Awin returned no usable products for this niche from your joined programmes.")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows[:CANDIDATES])
    return out_path


def _feed_products(feed_id: str, words: set) -> list[dict]:
    """Up to PRODUCTS_PER_PROGRAMME in-stock USD products from one feed, streamed so a large feed
    isn't downloaded whole."""
    url = FEED.format(key=os.environ["AWIN_FEED_KEY"], fid=feed_id, columns=COLUMNS)
    resp = http.request("GET", url, timeout=120, stream=True)
    found = []
    try:
        reader = csv.DictReader(io.TextIOWrapper(gzip.GzipFile(fileobj=resp.raw), encoding="utf-8-sig"))
        for item in reader:
            link = (item.get("aw_deep_link") or "").strip()
            if (not link.startswith("https://") or item.get("currency", "USD") != "USD"
                    or str(item.get("in_stock", "1")).strip() in ("0", "false", "no")):
                continue
            try:
                float(item["search_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if words and not words & _words(item.get("product_name", "")):
                continue
            found.append(item)
            if len(found) >= PRODUCTS_PER_PROGRAMME:
                break
    finally:
        resp.close()
    return found


def write_earnings(path: str, since: str, today: str) -> None:
    """Write the product_id,date,earnings,click_date CSV from Awin transactions (declined excluded),
    in 31-day windows, the longest Awin allows per request."""
    rows, start, end_all = [], date.fromisoformat(since), date.fromisoformat(today) + timedelta(days=1)
    while start < end_all:
        end = min(start + timedelta(days=31), end_all)
        for t in _get("/transactions/", startDate=f"{start.isoformat()}T00:00:00",
                      endDate=f"{end.isoformat()}T00:00:00", timezone="UTC") or []:
            ref = ((t.get("clickRefs") or {}).get("clickRef") or "").strip()
            if not ref or str(t.get("commissionStatus", "")).lower() == "declined":
                continue
            rows.append([ref, (t.get("transactionDate") or "")[:10],
                         (t.get("commissionAmount") or {}).get("amount", 0), (t.get("clickDate") or "")[:10]])
        start = end
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "date", "earnings", "click_date"])
        w.writerows(rows)


def join_list_lines(niche: str, limit: int = 15) -> list[str]:
    """Markdown lines ranking not-yet-joined Awin programmes for the niche by commission."""
    words = _words(niche)
    fitting = [p for p in programmes("notjoined")
               if words & _words(f"{p.get('name', '')} {p.get('description', '')}")][:DETAIL_LOOKUPS]
    ranked = sorted(((commission_rate(p["id"], "notjoined") or 0, p) for p in fitting),
                    key=lambda x: -x[0])[:limit]
    lines = [f"## Awin programmes for '{niche}'", "",
             "In Awin, open Advertisers > Join Programmes, search each ID and apply (some accept",
             "you instantly).", "", "| Advertiser | ID | Commission |", "|---|---|---|"]
    for rate, p in ranked:
        name = str(p.get("name", "")).replace("|", "/")
        site = p.get("displayUrl") or ""
        lines.append(f"| {f'[{name}]({site})' if site.startswith('http') else name} | {p['id']} | "
                     f"{f'{rate:.0%}' if rate else 'n/a'} |")
    return lines
