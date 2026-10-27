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
# A niche needs people searching for it on Pinterest; categories below this are passed over when
# Pinterest reports search volume. An assumption: Pinterest doesn't state the volume's period.
MIN_SEARCH_VOLUME = 1000


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
    programs, page = [], 1
    while True:
        resp = http.request("GET", LOOKUP, headers=_headers(), timeout=60, params={
            "requestor-cid": os.environ["CJ_COMPANY_ID"], "advertiser-ids": "joined",
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
                })
        if len(found) < 100:
            return programs
        page += 1


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
    searched = {c: s for c, s in scores.items() if volumes.get(c, 0) >= MIN_SEARCH_VOLUME}
    return max(searched or scores, key=(searched or scores).get)


def discover(niche: str | None, out_path: str, demand=None) -> tuple[str, str]:
    """Write a products CSV from joined CJ programs, best earners per click first.

    With no niche, picks one (pick_niche) and takes products from that category's programs."""
    programs = sorted(joined_programs(), key=lambda p: -(p["epc"] or 0))
    keywords = niche.split() if niche else []
    if not niche:
        niche = pick_niche(programs, demand)
        programs = [p for p in programs if p["category"] == niche]
    rows = []
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
        if len(rows) >= CANDIDATES:
            break
    if not rows:
        raise ValueError("CJ returned no usable products for this niche from your joined programs.")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows[:CANDIDATES])
    return out_path, niche


def write_earnings(path: str, since: str, today: str) -> None:
    """Rewrite the product_id,date,earnings CSV the autopilot reads, from CJ commissions."""
    rows, start = [], date.fromisoformat(since)
    while start <= date.fromisoformat(today):
        end = min(start + timedelta(days=30), date.fromisoformat(today) + timedelta(days=1))
        query = f"""{{ publisherCommissions(forPublishers: ["{os.environ['CJ_COMPANY_ID']}"],
            sincePostingDate: "{start.isoformat()}T00:00:00Z",
            beforePostingDate: "{end.isoformat()}T00:00:00Z") {{
          payloadComplete records {{ postingDate shopperId pubCommissionAmountUsd }} }} }}"""
        data = _graphql(COMMISSIONS, query)["publisherCommissions"]
        if not data.get("payloadComplete", True):
            raise ValueError("CJ returned a partial commission report; too many records for one call.")
        for r in data["records"]:
            if r.get("shopperId"):
                rows.append([r["shopperId"], r["postingDate"][:10], r["pubCommissionAmountUsd"]])
        start = end
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "date", "earnings"])
        w.writerows(rows)
