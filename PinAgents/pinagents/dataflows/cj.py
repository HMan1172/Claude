"""CJ Affiliate: finds products to promote and fetches what they earned, so nobody has to.

Works with the programs your CJ publisher account has joined (each advertiser approves its own
publishers, so joining stays with you). Needs CJ_API_TOKEN (a personal access token from
developers.cj.com), CJ_COMPANY_ID (your publisher CID) and CJ_WEBSITE_ID (the PID links track to).

  - Advertiser Lookup (REST, XML): joined programs with their sale commission and EPC.
  - shoppingProducts (GraphQL): products from those programs, with your tracking link.
  - publisherCommissions (GraphQL): earnings, matched to products through the link's sid.

Field names follow CJ's published API docs but have not been run against a live account;
anything unexpected raises an error instead of being guessed at.
"""
import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from . import http

LOOKUP = "https://advertiser-lookup.api.cj.com/v2/advertiser-lookup"
PRODUCTS = "https://ads.api.cj.com/query"
COMMISSIONS = "https://commissions.api.cj.com/query"
PRODUCTS_PER_PROGRAM = 25
CANDIDATES = 40  # products handed to the Product Scout
# A niche needs people searching for it on Pinterest. When Pinterest reports search volume,
# categories searched less than this share as often as the most-searched one are passed over.
# Relative, because Pinterest doesn't say what period or unit its volume figure uses.
MIN_SHARE_OF_TOP_SEARCHES = 0.1


def configured() -> bool:
    return all(os.getenv(k) for k in ("CJ_API_TOKEN", "CJ_COMPANY_ID", "CJ_WEBSITE_ID"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['CJ_API_TOKEN']}"}


def _graphql(url: str, query: str, explain: bool = True) -> dict:
    body = http.request("POST", url, json={"query": query}, headers=_headers(), timeout=60).json()
    if body.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in body["errors"])
        raise ValueError(f"CJ API error: {messages}" + (_schema_hint(url, messages) if explain else ""))
    return body["data"]


def _schema_hint(url: str, messages: str) -> str:
    """When CJ rejects a field or argument name, ask its schema what the real names are."""
    hints = []
    for name in dict.fromkeys(re.findall(r'on (?:type|field) "?([A-Za-z_][\w.]*)"?', messages)):
        type_name, _, field = name.partition(".")
        try:
            t = _graphql(url, f'{{ __type(name: "{type_name}") {{ fields {{ name args {{ name }} }} }} }}',
                         explain=False)["__type"]
        except Exception:
            continue
        fields = (t or {}).get("fields") or []
        if field:
            args = next((f["args"] for f in fields if f["name"] == field), [])
            hints.append(f"{name} takes: {', '.join(a['name'] for a in args)}")
        else:
            hints.append(f"{type_name} has: {', '.join(f['name'] for f in fields)}")
    return f" (CJ's schema says {'; '.join(hints)})" if hints else ""


def _sale_commission(advertiser) -> float | None:
    """Highest percentage commission among the program's sale actions, as a fraction."""
    rates = []
    for action in advertiser.iter("action"):
        if "sale" not in (action.findtext("type") or "").lower():
            continue
        for value in action.iter():
            m = re.search(r"([\d.]+)\s*%", value.text or "")
            if m:
                rates.append(float(m.group(1)) / 100)
    return max(rates) if rates else None


def _epc(text) -> float | None:
    """CJ reports EPC in dollars per 100 clicks; returns dollars per click."""
    try:
        return float(text) / 100
    except (TypeError, ValueError):
        return None


def joined_programs() -> list[dict]:
    return _programs("joined")


def _programs(relationship: str, max_pages: int = 1000) -> list[dict]:
    """Programs with a percentage sale commission; relationship is "joined" or "notjoined"."""
    programs, page = [], 1
    while page <= max_pages:
        resp = http.request("GET", LOOKUP, headers=_headers(), timeout=60, params={
            "requestor-cid": os.environ["CJ_COMPANY_ID"], "advertiser-ids": relationship,
            "records-per-page": 100, "page-number": page})
        found = ET.fromstring(resp.content).findall(".//advertiser")
        for a in found:
            rate = _sale_commission(a)
            if rate:
                programs.append({
                    "id": a.findtext("advertiser-id"), "name": a.findtext("advertiser-name") or "",
                    "commission_rate": rate,
                    "epc": _epc(a.findtext("three-month-epc")) or _epc(a.findtext("seven-day-epc")),
                    "category": (a.findtext("primary-category/child") or a.findtext("primary-category/parent") or "").strip(),
                    "site": (a.findtext("program-url") or "").strip(),
                })
        if len(found) < 100:
            break
        page += 1
    return programs


def write_join_list(niche: str, path: str, categories=(), limit: int = 20) -> int:
    """Rank programs you haven't joined by earnings per click, those in the niche first, and write
    them to a Markdown checklist. Joining stays a click in CJ: advertisers approve their publishers.

    A program fits the niche when its CJ category is one that supplied products for the niche
    (categories) or shares a word with the niche."""
    words = set(re.findall(r"[a-z0-9]+", niche.lower()))
    fits = lambda p: p["category"] in categories or bool(words & set(re.findall(r"[a-z0-9]+", p["category"].lower())))
    ranked = sorted(_programs("notjoined", max_pages=10),
                    key=lambda p: (not fits(p), -(p["epc"] or 0), -p["commission_rate"]))[:limit]
    lines = [f"# CJ programs worth joining for '{niche}'", "",
             "In CJ, open Advertisers, search each ID below and click Apply. Programs that accept you",
             "are used automatically from the next batch.", "",
             "| Advertiser | ID | Category | Commission | EPC per click | In niche |", "|---|---|---|---|---|---|"]
    for p in ranked:
        epc = f"${p['epc']:.2f}" if p["epc"] is not None else "n/a"
        clean = p["name"].replace("|", "/")  # a "|" would break the table
        name = f"[{clean}]({p['site']})" if p["site"].startswith("http") else clean
        lines.append(f"| {name} | {p['id']} | {p['category']} | {p['commission_rate']:.0%} | {epc} | "
                     f"{'yes' if fits(p) else ''} |")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(ranked)


def lookup_sample() -> str:
    """What CJ's program lookup actually returned, for when no program could be read from it."""
    resp = http.request("GET", LOOKUP, headers=_headers(), timeout=60, params={
        "requestor-cid": os.environ["CJ_COMPANY_ID"], "advertiser-ids": "joined",
        "records-per-page": 1, "page-number": 1})
    root = ET.fromstring(resp.content)
    found = root.findall(".//advertiser")
    if not found:
        return f"no <advertiser> elements; top-level tags: {[c.tag for c in root]}"
    return f"first <advertiser> has: {sorted({e.tag for e in found[0].iter()} - {'advertiser'})}"


def program_products(program: dict, keywords: list[str], photos: bool = True) -> list[dict]:
    keyword_arg = f"keywords: {json.dumps(keywords)}, " if keywords else ""
    query = f"""{{ shoppingProducts(companyId: "{os.environ['CJ_COMPANY_ID']}",
        partnerIds: ["{program['id']}"], partnerStatus: JOINED,
        {keyword_arg}limit: {PRODUCTS_PER_PROGRAM}) {{
      resultList {{ id title {"imageLink " if photos else ""}price {{ amount currency }}
        linkCode(pid: "{os.environ['CJ_WEBSITE_ID']}") {{ clickUrl }} }} }} }}"""
    try:
        return _graphql(PRODUCTS, query)["shoppingProducts"]["resultList"] or []
    except ValueError as err:
        if photos and "imageLink" in str(err):  # photos are a bonus; never lose the products over them
            return program_products(program, keywords, photos=False)
        raise


def pick_niche(programs: list[dict], demand=None) -> str:
    """The category whose joined programs earn the most per click (counting commission when EPC is
    missing), among those people actually search for on Pinterest when demand can be measured.

    demand: callable taking category names and returning {name: Pinterest search volume}."""
    scores = {}
    for p in programs:
        if p["category"]:
            scores[p["category"]] = max(scores.get(p["category"], 0), p["epc"] or p["commission_rate"] / 100)
    if not scores:
        raise ValueError("None of your joined CJ programs lists a category; give --niche.")
    volumes = demand(list(scores)) if demand else {}
    top = max(volumes.values(), default=0)
    searched = {c: s for c, s in scores.items() if top and volumes.get(c, 0) >= MIN_SHARE_OF_TOP_SEARCHES * top}
    return max(searched or scores, key=(searched or scores).get)


def discover(niche: str | None, out_path: str, demand=None) -> tuple[str, str, list[str]]:
    """Write a products CSV from joined CJ programs, best earners per click first.

    With no niche, picks one (pick_niche) and takes products from that category's programs.
    Returns the CSV path, the niche, and the CJ categories of the programs that supplied products
    (what the niche means in CJ's terms, for the join list)."""
    programs = sorted(joined_programs(), key=lambda p: -(p["epc"] or 0))
    keywords = niche.split() if niche else []
    if not niche:
        niche = pick_niche(programs, demand)
        programs = [p for p in programs if p["category"] == niche]
    rows, categories = [], []
    for program in programs:
        for item in program_products(program, keywords):
            url = ((item.get("linkCode") or {}).get("clickUrl") or "").strip()
            price = item.get("price") or {}
            if not url.startswith("https://") or price.get("currency", "USD") != "USD":
                continue
            pid = "cj" + re.sub(r"[^A-Za-z0-9]+", "", str(item["id"]))[:30]
            rows.append({
                "id": pid, "name": item["title"][:100],
                "url": f"{url}{'&' if '?' in url else '?'}sid={pid}",  # ties earnings to this product
                "price": float(price["amount"]), "commission_rate": program["commission_rate"],
                "program": program["name"], "notes": "found automatically on CJ",
                "epc": "" if program["epc"] is None else program["epc"],
                "image_url": (item.get("imageLink") or "").strip(),
            })
            if program["category"] and program["category"] not in categories:
                categories.append(program["category"])
        if len(rows) >= CANDIDATES:
            break
    if not rows:
        raise ValueError("CJ returned no usable products for this niche from your joined programs.")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows[:CANDIDATES])
    return out_path, niche, categories


def _commissions(start: date, end: date, click_dates: bool) -> dict:
    query = f"""{{ publisherCommissions(forPublishers: ["{os.environ['CJ_COMPANY_ID']}"],
        sincePostingDate: "{start.isoformat()}T00:00:00Z",
        beforePostingDate: "{end.isoformat()}T00:00:00Z") {{
      payloadComplete records {{ postingDate shopperId pubCommissionAmountUsd {"clickDate" if click_dates else ""} }} }} }}"""
    return _graphql(COMMISSIONS, query)["publisherCommissions"]


def write_earnings(path: str, since: str, today: str) -> None:
    """Rewrite the product_id,date,earnings,click_date CSV the autopilot reads, from CJ commissions.
    click_date (when CJ provides it) lets the autopilot measure how long sales take to post."""
    rows, click_dates = [], True
    windows, start, last = [], date.fromisoformat(since), date.fromisoformat(today) + timedelta(days=1)
    while start < last:
        windows.append((start, min(start + timedelta(days=30), last)))
        start = windows[-1][1]
    while windows:
        start, end = windows.pop(0)
        try:
            data = _commissions(start, end, click_dates)
        except ValueError as err:
            if not (click_dates and "clickDate" in str(err)):
                raise
            click_dates = False  # click dates are a bonus; never lose the earnings over them
            data = _commissions(start, end, click_dates)
        if not data.get("payloadComplete", True):
            if end - start <= timedelta(days=1):
                raise ValueError("CJ returned a partial commission report even for a single day.")
            mid = start + (end - start) / 2  # too many records for one call: ask for half at a time
            windows[:0] = [(start, mid), (mid, end)]
            continue
        for r in data["records"]:
            if r.get("shopperId"):
                rows.append([r["shopperId"], r["postingDate"][:10], r["pubCommissionAmountUsd"],
                             (r.get("clickDate") or "")[:10]])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "date", "earnings", "click_date"])
        w.writerows(rows)
