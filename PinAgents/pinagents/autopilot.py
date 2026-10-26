"""Autopilot: one unattended cycle of the whole account. Run it daily (cron) or with --loop.

Each cycle:
  1. Refreshes the Pinterest token so unattended runs never lock out.
  2. When no pins are waiting, has the agents write a new batch.
  3. Posts the pins that are due (at most pins_per_day).
  4. Ads: syncs real spend into BudgetGuard, pauses products that lose money or use up their
     test budget without proving themselves, authorizes today's budget, and promotes new pins.
  5. Once a week, turns ad results into lessons the agents read on the next batch.

Every rule that spends money is plain code. The agents write pins; they never set budgets or bids.
"""
import csv
import json
import os
from datetime import date, timedelta

from .budget_guard import BudgetGuard, SpendRefused
from .dataflows.pinterest_api import PinterestClient
from .dataflows.reports import TRACKING_COLUMNS
from .economics import max_cpc
from .graph.pin_graph import PinAgentsGraph
from .llm import LLM
from .output import save_run
from .tracker import MIN_CLICKS, analyze

STATS_DAYS = 89  # Pinterest analytics only go back 90 days
# Bid 40% of what a click should earn. On a fixed daily budget, lower bids buy cheaper clicks;
# experiments/simulate.py found 60% beat 30% across cheap, typical and expensive markets.
BID_MARGIN = 0.6


def _empty_state() -> dict:
    return {"queue": [], "posted": {}, "products": {}, "campaign": {"id": None, "status": None},
            "groups": {}, "ads": {}, "last_lessons": None}


def _dry_path(path: str) -> str:
    root, ext = os.path.splitext(path)
    return f"{root}-dry{ext}"


def _days_ago(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def run_cycle(niche: str, products_csv: str, config: dict, client=None, today: str | None = None) -> list[str]:
    today = today or date.today().isoformat()
    client = client or PinterestClient(config)
    log = []
    if client.refresh_access_token():
        log.append("Refreshed Pinterest access token.")

    # Dry runs keep their own state and ledger so they never mix with real ids or real spend.
    state_path = config["autopilot_state_path"] if client.live else _dry_path(config["autopilot_state_path"])
    guard_cfg = dict(config)
    if not client.ads_live:
        guard_cfg["ads_ledger_path"] = _dry_path(config["ads_ledger_path"])
    guard = BudgetGuard.from_config(guard_cfg)

    st = _empty_state()
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            st.update(json.load(f))
    try:
        if client.ads_live:
            _drop_dry_ads(st)
        if not st["queue"]:
            _generate(st, niche, products_csv, config, today, log)
        _post_due(st, client, config, today, log)
        stats = _run_ads(st, client, guard, config, today, log)
        if stats and (not st["last_lessons"] or st["last_lessons"] <= _days_ago(today, 7)):
            _learn(st, stats, niche, config, today, log)
    finally:
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
    if not client.live or not client.ads_live:
        log.append(f"Dry run (posting live: {client.live}, ads live: {client.ads_live}).")
    return log


def _drop_dry_ads(st: dict) -> None:
    """Forget ad ids made up during a dry run once ads go live."""
    if (st["campaign"]["id"] or "").startswith("dry-"):
        st["campaign"] = {"id": None, "status": None}
    st["groups"] = {k: g for k, g in st["groups"].items() if not (g.get("id") or "").startswith("dry-")}
    st["ads"] = {k: a for k, a in st["ads"].items() if not a.startswith("dry-")}


def _generate(st, niche, products_csv, config, today, log):
    state = PinAgentsGraph(config).propagate(niche, products_csv, run_date=today)
    run_dir = save_run(state, config)
    for s in state.get("shortlist", []):
        st["products"][s["id"]] = {"name": s["name"], "expected_commission": s["expected_commission"],
                                   "domain": s["domain"], "conversion_rate": s.get("conversion_rate")}
    pins = state.get("final", {}).get("pins", [])
    for p in pins:
        st["queue"].append({
            "key": f"{today}:{p['key']}", "product_id": p["product_id"], "title": p["title"],
            "description": p["description"], "link": p["link"], "alt_text": p["alt_text"],
            "post_date": p["post_date"], "image": os.path.abspath(os.path.join(run_dir, p["image"])),
        })
    log.append(f"Agents wrote {len(pins)} pins ({run_dir}).")


def _post_due(st, client, config, today, log):
    due = [p for p in st["queue"] if p["post_date"] <= today][:config["pins_per_day"]]
    for p in due:
        res = client.create_pin(p, p["image"])
        st["posted"][p["key"]] = {**p, "pin_id": res["id"], "posted_date": today}
        st["queue"].remove(p)
    if due:
        log.append(f"Posted {len(due)} pins, {len(st['queue'])} waiting.")


def _set_campaign(st, client, status, why, log):
    c = st["campaign"]
    if c["id"] and c["status"] != status:
        client.set_status("campaigns", [c["id"]], status)
        c["status"] = status
        log.append(f"Campaign {status.lower()}: {why}")


def _load_earnings(path: str, since: str) -> dict:
    """{product_id: dollars} from a product_id,date,earnings CSV your affiliate network exports."""
    if not path or not os.path.exists(path):
        return {}
    totals = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("date") or since) >= since:
                pid = (row.get("product_id") or "").strip()
                value = (row.get("earnings") or "0").strip().replace("$", "").replace(",", "")
                totals[pid] = totals.get(pid, 0.0) + float(value or 0)
    return totals


def _verdict(spend: float, clicks: float, earned, config) -> str:
    """Why a product's ads should stop, or "" to keep going."""
    if earned is not None and clicks >= MIN_CLICKS:
        return f"losing money (spent ${spend:.2f}, earned ${earned:.2f})" if earned < spend else ""
    if spend >= config["ads_test_spend_per_product"]:
        return f"spent ${spend:.2f} without proving it earns more than it costs"
    return ""


def _is_amazon(domain: str) -> bool:
    """Amazon links: since the April 14, 2026 Associates agreement, ad-referred sales earn nothing."""
    return any(part == "amazon" for part in domain.lower().split(".")) or domain.lower().endswith("amzn.to")


def _run_ads(st, client, guard, config, today, log) -> dict:
    if os.path.exists(config["kill_switch_path"]):
        _set_campaign(st, client, "PAUSED", "kill switch is on", log)
        return {}
    cid = st["campaign"]["id"]
    since = _days_ago(today, STATS_DAYS)

    # 1. Replace past days' authorized budgets with what Pinterest really charged.
    if cid and client.ads_live:
        spent = client.campaign_daily_spend(cid, _days_ago(today, 7), today)
        for i in range(1, 8):
            day = _days_ago(today, i)
            guard.settle(day, spent.get(day, 0.0))

    # 2. Score each advertised product; pause losers.
    stats = client.ad_stats(list(st["ads"].values()), since, today)
    by_product = {}
    for key, ad_id in st["ads"].items():
        s, pid = stats.get(ad_id), st["posted"][key]["product_id"]
        if s:
            t = by_product.setdefault(pid, {"spend": 0.0, "clicks": 0})
            t["spend"] += s["spend"]
            t["clicks"] += s["clicks"]
    earnings = _load_earnings(config["earnings_csv"], since)
    for pid, g in st["groups"].items():
        if g["status"] != "ACTIVE":
            continue
        t = by_product.get(pid, {"spend": 0.0, "clicks": 0})
        why = _verdict(t["spend"], t["clicks"], earnings.get(pid), config)
        if why:
            client.set_status("ad_groups", [g["id"]], "PAUSED")
            g.update(status="PAUSED", reason=why)
            log.append(f"Paused ads for {pid}: {why}.")

    # 3. Authorize today's budget for ad groups already running.
    active = [g for g in st["groups"].values() if g["status"] == "ACTIVE"]
    needed = sum(g["budget"] for g in active) - guard.spent_today(today)
    if needed > 0.005:
        try:
            guard.authorize(round(needed, 2), today)
        except SpendRefused as err:
            _set_campaign(st, client, "PAUSED", str(err), log)
            return stats
    _set_campaign(st, client, "ACTIVE", "within budget", log)

    # 4. Promote posted pins that don't have an ad yet.
    budget = config["ad_group_daily_budget"]
    for key, p in st["posted"].items():
        if key in st["ads"]:
            continue
        pid = p["product_id"]
        if pid not in st["groups"] and _is_amazon(st["products"].get(pid, {}).get("domain", "")):
            st["groups"][pid] = {"status": "SKIPPED", "reason": "Amazon pays no commission on sales from paid ads"}
            log.append(f"Not advertising {pid}: {st['groups'][pid]['reason']}.")
            continue
        if pid not in st["groups"]:
            prod = st["products"].get(pid, {})
            cr = prod.get("conversion_rate") or config["assumed_conversion_rate"]
            epc = cr * prod.get("expected_commission", 0)
            bid = round(max_cpc(epc, BID_MARGIN), 2)
            if bid < config["ads_min_bid"]:
                st["groups"][pid] = {"status": "SKIPPED",
                                     "reason": f"can only afford ${bid:.2f} per click, minimum is ${config['ads_min_bid']:.2f}"}
                log.append(f"Not advertising {pid}: {st['groups'][pid]['reason']}.")
                continue
            try:
                guard.authorize(budget, today)
            except SpendRefused as err:
                log.append(f"No budget for new ads today: {err}")
                break
            if not cid:
                cid = client.create_campaign(f"PinAgents {today}", config["ads_total_cap"])
                st["campaign"] = {"id": cid, "status": "ACTIVE"}
            st["groups"][pid] = {"id": client.create_ad_group(cid, f"PinAgents {pid}", budget, bid),
                                 "status": "ACTIVE", "budget": budget, "bid": bid}
            log.append(f"Started ads for {pid}: ${budget:.2f}/day, max ${bid:.2f} per click.")
        if st["groups"][pid]["status"] == "ACTIVE":
            st["ads"][key] = client.create_ad(st["groups"][pid]["id"], p["pin_id"], key)
    return stats


def _learn(st, stats, niche, config, today, log):
    """Write ad results as a tracking sheet and let the Performance Analyst draw lessons from it."""
    earnings = _load_earnings(config["earnings_csv"], _days_ago(today, STATS_DAYS))
    clicks_by_product = {}
    for key, ad_id in st["ads"].items():
        pid = st["posted"][key]["product_id"]
        clicks_by_product[pid] = clicks_by_product.get(pid, 0) + stats.get(ad_id, {}).get("clicks", 0)
    rows = []
    for key, ad_id in st["ads"].items():
        p, s = st["posted"][key], stats.get(ad_id)
        if not s:
            continue
        share = s["clicks"] / clicks_by_product[p["product_id"]] if clicks_by_product[p["product_id"]] else 0
        rows.append({"pin_key": key, "product_id": p["product_id"], "title": p["title"],
                     "posted_date": p["posted_date"], "impressions": s["impressions"],
                     "outbound_clicks": s["clicks"], "saves": 0,
                     "earnings": round(earnings.get(p["product_id"], 0.0) * share, 2)})
    if not rows:
        return
    os.makedirs(config["results_dir"], exist_ok=True)
    sheet = os.path.join(config["results_dir"], f"autopilot_tracking_{today}.csv")
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACKING_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    analyze(sheet, niche, LLM(config), config)
    st["last_lessons"] = today
    log.append(f"Saved lessons from {len(rows)} ads ({sheet}).")
