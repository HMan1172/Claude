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
import time
from datetime import date, timedelta

from . import http
from .http import NETWORK_ERRORS

API = "https://api.awin.com"
FEED_LIST = "https://ui.awin.com/productdata-darwin-download/publisher/{pub}/{key}/1/feedList"
FEED = ("https://productdata.awin.com/datafeed/download/apikey/{key}/fid/{fid}/columns/{columns}"
        "/format/csv/delimiter/%2C/compression/gzip/")
COLUMNS = "aw_deep_link,product_name,aw_product_id,search_price,currency,merchant_id,merchant_name,aw_image_url,in_stock"
PRODUCTS_PER_PROGRAMME = 25
CANDIDATES = 40
DETAIL_LOOKUPS = 15  # programmedetails calls per discover or join list
MAX_ROWS_SCANNED = 20_000  # per feed; some feeds hold hundreds of thousands of products
MAX_FEEDS_SCANNED = 20  # per discover, so a niche matching nothing can't mean downloading every feed
API_SPACING = 3.1  # seconds between api.awin.com calls: Awin allows 20 a minute
DROPPED_STATUSES = {"declined", "deleted"}
_last_call = [0.0]


def configured() -> bool:
    return all(os.getenv(k) for k in ("AWIN_API_TOKEN", "AWIN_PUBLISHER_ID", "AWIN_FEED_KEY"))


def _get(path: str, **params):
    wait = _last_call[0] + API_SPACING - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
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
    """Write a products CSV from joined Awin programmes' feeds: in-stock USD products whose name
    shares a word with the niche, or any of their products when no name matches at all.
    Commission is looked up only for programmes with usable products, and one broken feed is
    skipped rather than stopping the rest."""
    feeds = list(csv.DictReader(io.StringIO(http.request(
        "GET", FEED_LIST.format(pub=os.environ["AWIN_PUBLISHER_ID"], key=os.environ["AWIN_FEED_KEY"]),
        timeout=60).text)))
    words = _words(niche)
    matched, others, rates, lookups, scanned = [], [], {}, 0, 0
    for f in feeds:
        adv = str(f.get("Advertiser ID", ""))
        if f.get("Membership Status") != "active" or f.get("Datafeed Format") == "Google":
            continue
        if (adv in rates and not rates[adv]) or (adv not in rates and lookups >= DETAIL_LOOKUPS):
            continue  # no usable commission, or no lookups left: don't download its feed
        if scanned >= MAX_FEEDS_SCANNED or len(matched) >= CANDIDATES:
            break
        scanned += 1
        try:
            hits, rest = _feed_products(f["Feed ID"].lstrip("Ff"), words)
            if (hits or rest) and adv not in rates:
                lookups += 1
                rates[adv] = commission_rate(adv)
        except NETWORK_ERRORS:  # one broken feed or lookup mustn't stop the rest
            continue
        if rates.get(adv):
            matched += [_row(item, rates[adv]) for item in hits]
            others += [_row(item, rates[adv]) for item in rest]
    rows = (matched or others)[:CANDIDATES]
    if not rows:
        raise ValueError("Awin returned no usable products from your joined programmes.")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return out_path


def _row(item: dict, rate: float) -> dict:
    pid = "aw" + re.sub(r"[^A-Za-z0-9]+", "", item["aw_product_id"])[:30]
    link = item["aw_deep_link"].strip()
    return {
        "id": pid, "name": item["product_name"][:100],
        "url": f"{link}{'&' if '?' in link else '?'}clickref={pid}",  # ties earnings to this product
        "price": float(item["search_price"]), "commission_rate": rate,
        "program": item.get("merchant_name", ""), "notes": "found automatically on Awin",
        "epc": "", "image_url": (item.get("aw_image_url") or "").strip(),
    }


def _feed_products(feed_id: str, words: set) -> tuple[list[dict], list[dict]]:
    """In-stock USD products from one feed: (those matching the niche, up to as many others),
    streamed and capped so a huge feed isn't read whole."""
    url = FEED.format(key=os.environ["AWIN_FEED_KEY"], fid=feed_id, columns=COLUMNS)
    resp = http.request("GET", url, timeout=120, stream=True)
    hits, rest = [], []
    try:
        reader = csv.DictReader(io.TextIOWrapper(gzip.GzipFile(fileobj=resp.raw), encoding="utf-8-sig"))
        for n, item in enumerate(reader):
            if n >= MAX_ROWS_SCANNED or len(hits) >= PRODUCTS_PER_PROGRAMME:
                break
            link = (item.get("aw_deep_link") or "").strip()
            if (not link.startswith("https://") or item.get("currency", "USD") != "USD"
                    or str(item.get("in_stock", "1")).strip().lower() in ("0", "false", "no")):
                continue
            try:
                float(item["search_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if not words or words & _words(item.get("product_name", "")):
                hits.append(item)
            elif len(rest) < PRODUCTS_PER_PROGRAMME:
                rest.append(item)
    finally:
        resp.close()
    return hits, rest


def write_earnings(path: str, since: str, today: str) -> int:
    """Write the product_id,date,earnings,click_date CSV from Awin transactions in 31-day windows,
    the longest Awin allows per request. Declined and deleted ones are left out, and so are ones
    not in USD (returned as a count): they can't be weighed against Pinterest's USD spend."""
    rows, skipped = [], 0
    start, end_all = date.fromisoformat(since), date.fromisoformat(today) + timedelta(days=1)
    while start < end_all:
        end = min(start + timedelta(days=31), end_all)
        for t in _get("/transactions/", startDate=f"{start.isoformat()}T00:00:00",
                      endDate=f"{end.isoformat()}T00:00:00", timezone="UTC") or []:
            ref = ((t.get("clickRefs") or {}).get("clickRef") or "").strip()
            if not ref or str(t.get("commissionStatus", "")).lower() in DROPPED_STATUSES:
                continue
            if (t.get("commissionAmount") or {}).get("currency", "USD") != "USD":
                skipped += 1
                continue
            rows.append([ref, (t.get("transactionDate") or "")[:10],
                         (t.get("commissionAmount") or {}).get("amount", 0), (t.get("clickDate") or "")[:10]])
        start = end
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "date", "earnings", "click_date"])
        w.writerows(rows)
    return skipped


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
