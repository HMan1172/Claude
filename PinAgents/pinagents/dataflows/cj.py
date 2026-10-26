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

import requests

LOOKUP = "https://advertiser-lookup.api.cj.com/v2/advertiser-lookup"
PRODUCTS = "https://ads.api.cj.com/query"
COMMISSIONS = "https://commissions.api.cj.com/query"
PRODUCTS_PER_PROGRAM = 25
CANDIDATES = 40  # products handed to the Product Scout


def configured() -> bool:
    return all(os.getenv(k) for k in ("CJ_API_TOKEN", "CJ_COMPANY_ID", "CJ_WEBSITE_ID"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['CJ_API_TOKEN']}"}


def _graphql(url: str, query: str) -> dict:
    resp = requests.post(url, json={"query": query}, headers=_headers(), timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise ValueError(f"CJ API error: {body['errors']}")
    return body["data"]


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
        resp = requests.get(LOOKUP, headers=_headers(), timeout=60, params={
            "requestor-cid": os.environ["CJ_COMPANY_ID"], "advertiser-ids": "joined",
            "records-per-page": 100, "page-number": page})
        resp.raise_for_status()
        found = ET.fromstring(resp.content).findall(".//advertiser")
        for a in found:
            rate = _sale_commission(a)
            if rate:
                programs.append({
                    "id": a.findtext("advertiser-id"), "name": a.findtext("advertiser-name") or "",
                    "commission_rate": rate,
                    "epc": _epc(a.findtext("three-month-epc")) or _epc(a.findtext("seven-day-epc")),
                })
        if len(found) < 100:
            return programs
        page += 1


def program_products(program: dict, keywords: list[str]) -> list[dict]:
    query = f"""{{ shoppingProducts(companyId: "{os.environ['CJ_COMPANY_ID']}",
        partnerIds: ["{program['id']}"], partnerStatus: JOINED,
        keywords: {json.dumps(keywords)}, limit: {PRODUCTS_PER_PROGRAM}) {{
      resultList {{ id title price {{ amount currency }}
        linkCode(pid: "{os.environ['CJ_WEBSITE_ID']}") {{ clickUrl }} }} }} }}"""
    return _graphql(PRODUCTS, query)["shoppingProducts"]["resultList"] or []


def discover(niche: str, out_path: str) -> str:
    """Write a products CSV from joined CJ programs, best earners per click first."""
    programs = sorted(joined_programs(), key=lambda p: -(p["epc"] or 0))
    rows = []
    for program in programs:
        for item in program_products(program, niche.split()):
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
    return out_path


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
