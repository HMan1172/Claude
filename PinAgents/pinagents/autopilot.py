"""Autopilot: one unattended cycle of the whole account. Run it daily (cron) or with --loop.

Each cycle:
  1. Refreshes the Pinterest token so unattended runs never lock out.
  2. When no pins are waiting, has the agents write a new batch (from products found on CJ when
     no products CSV is given).
  3. Posts the pins that are due (at most pins_per_day).
  4. Ads: syncs real spend into BudgetGuard, pauses products that lose money or use up their
     test budget without proving themselves (judged on spend old enough for its sales to have
     posted), authorizes today's budget, and promotes new pins.
  5. Once a week, turns ad results into lessons the agents read on the next batch.

Every rule that spends money is plain code. The agents write pins; they never set budgets or bids.
"""
import csv
import json
import os
from datetime import date, timedelta

from .budget_guard import BudgetGuard, SpendRefused, write_json
from .dataflows import cj
from .dataflows.pinterest_api import PinterestClient
from .dataflows.reports import TRACKING_COLUMNS
from .economics import max_cpc
from .graph.pin_graph import PinAgentsGraph
from .llm import LLM
from .output import save_run
from .tracker import MIN_CLICKS, analyze

STATS_DAYS = 89  # Pinterest analytics only go back 90 days
# Network EPC comes from all publishers' traffic, which often buys more readily than Pinterest ad
# clicks. Bids are scaled down by what our own ads have earned against it, pooled across products
# and weighted like this many average sales of evidence, so a few unlucky days can't crash bids.
CALIBRATION_SALES = 5
# Bid 40% of what a click should earn. On a fixed daily budget, lower bids buy cheaper clicks;
# experiments/simulate.py found 60% beat 30% across cheap, typical and expensive markets.
BID_MARGIN = 0.6
# Sales reach the affiliate report days after the click (purchase delay, then posting). In
# simulation, judging products on spend this old beat judging on today's spend on a held-out seed.
ATTRIBUTION_DAYS = 4
EMPTY_BATCH_WAIT_DAYS = 3  # after a batch where every pin failed compliance


def _empty_state() -> dict:
    return {"queue": [], "posted": {}, "products": {}, "campaign": {"id": None, "status": None},
            "groups": {}, "ads": {}, "last_lessons": None}


def _dry_path(path: str) -> str:
    root, ext = os.path.splitext(path)
    return f"{root}-dry{ext}"


def _days_ago(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def run_cycle(niche: str | None, products_csv: str | None, config: dict, client=None,
              today: str | None = None) -> list[str]:
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
    def save():  # after every Pinterest create, so a crash can't lead to posting something twice
        write_json(state_path, st)

    try:
        if client.ads_live:
            _drop_dry_ads(st)
        niche = niche or st.get("niche")  # an automatically picked niche sticks between batches
        empty = st.get("empty_batch")
        if not st["queue"] and empty and empty > _days_ago(today, EMPTY_BATCH_WAIT_DAYS):
            log.append(f"Last batch had no pins pass compliance; next try after {EMPTY_BATCH_WAIT_DAYS} days.")
        elif not st["queue"]:
            if not products_csv:
                products_csv, niche = cj.discover(niche, os.path.join(config["results_dir"], f"cj_products_{today}.csv"))
                log.append(f"Found products on CJ for '{niche}' ({products_csv}).")
            _generate(st, niche, products_csv, config, today, log)
            st["empty_batch"] = None if st["queue"] else today
        st["niche"] = niche
        _post_due(st, client, config, today, log, save)
        if cj.configured():
            config = {**config, "earnings_csv": config["earnings_csv"] or config["cj_earnings_path"]}
            cj.write_earnings(config["earnings_csv"], _days_ago(today, STATS_DAYS), today)
        stats = _run_ads(st, client, guard, config, today, log, save)
        if stats and (not st["last_lessons"] or st["last_lessons"] <= _days_ago(today, 7)):
            _learn(st, stats, niche, config, today, log)
    finally:
        save()
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
                                   "domain": s["domain"], "conversion_rate": s.get("conversion_rate"),
                                   "epc": s.get("epc")}
    pins = state.get("final", {}).get("pins", [])
    for p in pins:
        st["queue"].append({
            "key": f"{today}:{p['key']}", "product_id": p["product_id"], "title": p["title"],
            "description": p["description"], "link": p["link"], "alt_text": p["alt_text"],
            "post_date": p["post_date"], "image": os.path.abspath(os.path.join(run_dir, p["image"])),
        })
    log.append(f"Agents wrote {len(pins)} pins ({run_dir}).")


def _post_due(st, client, config, today, log, save=lambda: None):
    due = [p for p in st["queue"] if p["post_date"] <= today][:config["pins_per_day"]]
    for p in due:
        res = client.create_pin(p, p["image"])
        st["posted"][p["key"]] = {**p, "pin_id": res["id"], "posted_date": today}
        st["queue"].remove(p)
        save()
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


def _calibrate_bids(st, client, by_product, earnings, config, min_bid: float) -> None:
    """Lower every bid by the pooled ratio of ad earnings to what network EPC predicted (never raise)."""
    expected = earned = 0.0
    commissions = []
    for pid, g in st["groups"].items():
        if "id" not in g:
            continue
        prod = st["products"].get(pid, {})
        expected += by_product.get(pid, {"clicks": 0})["clicks"] * _base_epc(prod, config)
        earned += _ad_earnings(earnings, pid, g) or 0.0
        commissions.append(prod.get("expected_commission", 0))
    prior = CALIBRATION_SALES * (sum(commissions) / len(commissions) if commissions else 0)
    st["epc_factor"] = min(1.0, (earned + prior) / (expected + prior)) if prior else 1.0
    for pid, g in st["groups"].items():
        if g["status"] != "ACTIVE":
            continue
        # Stopping a product stays with _verdict; calibration only moves bids, never below the minimum.
        bid = max(min_bid,
                  round(max_cpc(_base_epc(st["products"].get(pid, {}), config) * st["epc_factor"], BID_MARGIN), 2))
        if bid != g["bid"]:
            client.update_ad_group(g["id"], max_cpc=bid)
            g["bid"] = bid


def _base_epc(prod: dict, config) -> float:
    """Expected earnings per click before our own results: network EPC, else conversion x commission."""
    cr = prod.get("conversion_rate") or config["assumed_conversion_rate"]
    return prod.get("epc") or cr * prod.get("expected_commission", 0)


def _ad_link(link: str, pid: str) -> str | None:
    """The pin's link with its sid tag changed to "<id>-ad", so ad sales are reported apart from
    free-pin sales. None when the link carries no sid tag to change."""
    tag = f"sid={pid}"
    if tag not in link or f"{tag}-" in link:
        return None
    return link.replace(tag, f"{tag}-ad")


def _ad_earnings(earnings: dict, pid: str, group: dict):
    """Ad-driven earnings for a product: its "-ad" sid when the ad link was tagged."""
    return earnings.get(f"{pid}-ad") if group.get("ad_tagged") else earnings.get(pid)


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


def _run_ads(st, client, guard, config, today, log, save=lambda: None) -> dict:
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
    # Products are judged on spend from ATTRIBUTION_DAYS ago, so sales still on their way to the
    # affiliate report aren't missing when a product is weighed against its cost.
    judged = client.ad_stats(list(st["ads"].values()), since, _days_ago(today, ATTRIBUTION_DAYS))
    by_product = {}
    for key, ad_id in st["ads"].items():
        s, pid = judged.get(ad_id), st["posted"][key]["product_id"]
        if s:
            t = by_product.setdefault(pid, {"spend": 0.0, "clicks": 0})
            t["spend"] += s["spend"]
            t["clicks"] += s["clicks"]
    earnings = _load_earnings(config["earnings_csv"], since)
    for pid, g in st["groups"].items():
        if g["status"] != "ACTIVE":
            continue
        t = by_product.get(pid, {"spend": 0.0, "clicks": 0})
        why = _verdict(t["spend"], t["clicks"], _ad_earnings(earnings, pid, g), config)
        if why:
            client.set_status("ad_groups", [g["id"]], "PAUSED")
            g.update(status="PAUSED", reason=why)
            log.append(f"Paused ads for {pid}: {why}.")

    # Pinterest's real minimum bid for our targeting; the configured one when it won't say.
    min_bid = client.bid_floor() or config["ads_min_bid"]
    _calibrate_bids(st, client, by_product, earnings, config, min_bid)

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
            epc = _base_epc(st["products"].get(pid, {}), config) * st.get("epc_factor", 1.0)
            bid = round(max_cpc(epc, BID_MARGIN), 2)
            if bid < min_bid:
                st["groups"][pid] = {"status": "SKIPPED",
                                     "reason": f"can only afford ${bid:.2f} per click, minimum is ${min_bid:.2f}"}
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
                save()
            st["groups"][pid] = {"id": client.create_ad_group(cid, f"PinAgents {pid}", budget, bid),
                                 "status": "ACTIVE", "budget": budget, "bid": bid,
                                 "ad_tagged": _ad_link(p["link"], pid) is not None}
            save()
            log.append(f"Started ads for {pid}: ${budget:.2f}/day, max ${bid:.2f} per click.")
        if st["groups"][pid]["status"] == "ACTIVE":
            st["ads"][key] = client.create_ad(st["groups"][pid]["id"], p["pin_id"], key,
                                              destination_url=_ad_link(p["link"], pid))
            save()
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
                     "earnings": round((_ad_earnings(earnings, p["product_id"],
                                                     st["groups"][p["product_id"]]) or 0.0) * share, 2)})
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
