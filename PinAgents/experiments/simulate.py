"""Runs the autopilot's real ad logic against a simulated Pinterest market.

Nothing here is real data. Every range below is an assumption, so treat results as
"how the rules behave if the world looks like this", not as a profit forecast.

  python experiments/simulate.py                  # all scenarios, 200 trials each
  python experiments/simulate.py --trials 50 --seed 7 --set ads_test_spend_per_product=5
"""
import argparse
import csv
import math
import os
import random
import sys
import tempfile
from datetime import date, timedelta
from statistics import NormalDist

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pinagents.autopilot import _empty_state, _run_ads  # noqa: E402
from pinagents.budget_guard import BudgetGuard  # noqa: E402
from pinagents.default_config import DEFAULT_CONFIG  # noqa: E402

N = NormalDist()

# Each product: price, commission rate, true share of clicks that buy, order size vs list price.
# Market: median cost per click, day-to-day auction spread, auctions available per day.
SCENARIOS = {
    # Amazon-style: low commission, but any purchase within 24h counts, so conversion is higher.
    "amazon": dict(price=(15, 80), rate=(0.03, 0.045), cr=(0.03, 0.10), cart=(1.0, 1.8)),
    # Brand programs: higher commission, ordinary conversion.
    "brand": dict(price=(25, 150), rate=(0.08, 0.20), cr=(0.01, 0.04), cart=(1.0, 1.2)),
    # Expensive items with big commissions that rarely sell.
    "high_ticket": dict(price=(100, 400), rate=(0.15, 0.40), cr=(0.003, 0.015), cart=(1.0, 1.0)),
    # Pessimistic: modest commission, poor conversion.
    "weak": dict(price=(15, 60), rate=(0.04, 0.10), cr=(0.002, 0.01), cart=(1.0, 1.1)),
}
MARKET = dict(median_cpc=(0.15, 0.70), spread=0.6, auctions=(50, 400))


class SimMarket:
    """Implements the PinterestClient ads methods the autopilot calls."""
    live = ads_live = True

    def __init__(self, products: dict, rng: random.Random, earnings_csv: str):
        self.products, self.rng, self.earnings_csv = products, rng, earnings_csv
        self.groups, self.ads, self.daily_spend = {}, {}, {}
        self.campaign_active, self.n = True, 0
        with open(earnings_csv, "w", newline="") as f:
            csv.writer(f).writerow(["product_id", "date", "earnings"])

    def _id(self, kind):
        self.n += 1
        return f"{kind}{self.n}"

    def create_campaign(self, name, lifetime_cap):
        return self._id("c")

    def create_ad_group(self, campaign_id, name, daily_budget, max_cpc):
        gid = self._id("g")
        self.groups[gid] = {"pid": name.split()[-1], "budget": daily_budget, "bid": max_cpc, "active": True}
        return gid

    def create_ad(self, ad_group_id, pin_id, name):
        aid = self._id("ad")
        self.ads[aid] = {"gid": ad_group_id, "spend": 0.0, "clicks": 0, "impressions": 0}
        return aid

    def set_status(self, kind, ids, status):
        if kind == "campaigns":
            self.campaign_active = status == "ACTIVE"
        for i in ids:
            if i in self.groups:
                self.groups[i]["active"] = status == "ACTIVE"

    def ad_stats(self, ad_ids, start, end):
        return {a: dict(self.ads[a]) for a in ad_ids if a in self.ads}

    def campaign_daily_spend(self, campaign_id, start, end):
        return dict(self.daily_spend)

    def run_day(self, day: str) -> tuple[float, float]:
        """Deliver one day of clicks for every active ad; returns (spend, earnings)."""
        spend_total = earned_total = 0.0
        rows = []
        for aid, ad in self.ads.items():
            g = self.groups[ad["gid"]]
            if not (self.campaign_active and g["active"]):
                continue
            p = self.products[g["pid"]]
            # Second-price style auction: win when our bid beats the clearing price, pay that price.
            mu, s = math.log(p["median_cpc"]), MARKET["spread"]
            p_win = N.cdf((math.log(g["bid"]) - mu) / s)
            spend = 0.0
            clicks = 0
            for _ in range(p["auctions"]):
                if self.rng.random() >= p_win:
                    continue
                price = math.exp(mu + s * N.inv_cdf(max(1e-12, self.rng.random() * p_win)))
                if spend + price > g["budget"]:
                    break
                spend += price
                clicks += 1
            earned = 0.0
            for _ in range(clicks):
                if self.rng.random() < p["cr"]:
                    earned += p["price"] * self.rng.uniform(*p["cart"]) * p["rate"]
            ad["spend"] += spend
            ad["clicks"] += clicks
            ad["impressions"] += clicks * 50
            spend_total += spend
            earned_total += earned
            if earned:
                rows.append([g["pid"], day, round(earned, 2)])
        self.daily_spend[day] = round(spend_total, 2)
        with open(self.earnings_csv, "a", newline="") as f:
            csv.writer(f).writerows(rows)
        return spend_total, earned_total


def make_products(scenario: dict, rng: random.Random, n: int) -> dict:
    out = {}
    for i in range(n):
        out[f"p{i}"] = {
            "price": rng.uniform(*scenario["price"]), "rate": rng.uniform(*scenario["rate"]),
            "cr": rng.uniform(*scenario["cr"]), "cart": scenario["cart"],
            "median_cpc": rng.uniform(*MARKET["median_cpc"]),
            "auctions": rng.randint(*MARKET["auctions"]),
        }
    return out


def trial(scenario: dict, rng: random.Random, overrides: dict, days: int, n_products: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        config = {**DEFAULT_CONFIG, **overrides,
                  "kill_switch_path": os.path.join(tmp, "STOP"),
                  "ads_ledger_path": os.path.join(tmp, "ledger.json"),
                  "earnings_csv": os.path.join(tmp, "earnings.csv")}
        products = make_products(scenario, rng, n_products)
        market = SimMarket(products, rng, config["earnings_csv"])
        guard = BudgetGuard.from_config(config)
        st = _empty_state()
        for pid, p in products.items():
            st["products"][pid] = {"name": pid, "expected_commission": round(p["price"] * p["rate"], 2)}
            st["posted"][f"{pid}:v1"] = {"product_id": pid, "pin_id": f"pin-{pid}"}
        start = date(2026, 1, 1)
        spend = earned = 0.0
        for d in range(days):
            day = (start + timedelta(days=d)).isoformat()
            _run_ads(st, market, guard, config, day, [])
            s, e = market.run_day(day)
            spend += s
            earned += e
        # Ceiling: what a perfect-knowledge advertiser could earn per click above cost.
        good = sum(1 for p in products.values()
                   if p["cr"] * p["price"] * sum(p["cart"]) / 2 * p["rate"] > p["median_cpc"])
        return {"spend": spend, "earned": earned, "profit": earned - spend, "good_products": good}


def run(scenarios, trials, seed, overrides, days=60, n_products=5) -> dict:
    results = {}
    for name in scenarios:
        rng = random.Random(f"{seed}-{name}")
        rs = [trial(SCENARIOS[name], rng, overrides, days, n_products) for _ in range(trials)]
        profits = sorted(r["profit"] for r in rs)
        results[name] = {
            "mean_profit": sum(profits) / trials,
            "p10": profits[trials // 10], "median": profits[trials // 2], "p90": profits[trials * 9 // 10],
            "win_rate": sum(p > 0 for p in profits) / trials,
            "mean_spend": sum(r["spend"] for r in rs) / trials,
            "mean_earned": sum(r["earned"] for r in rs) / trials,
            "good_products": sum(r["good_products"] for r in rs) / trials,
        }
    return results


def _parse_set(items):
    out = {}
    for item in items:
        k, v = item.split("=", 1)
        out[k] = float(v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--scenario", action="append", choices=list(SCENARIOS))
    ap.add_argument("--set", action="append", default=[], help="config override, e.g. ads_min_bid=0.05")
    args = ap.parse_args()
    res = run(args.scenario or list(SCENARIOS), args.trials, args.seed, _parse_set(args.set), args.days)
    print(f"{'scenario':12} {'mean':>8} {'p10':>8} {'median':>8} {'p90':>8} {'win%':>5} "
          f"{'spend':>7} {'earned':>7} {'good/5':>6}")
    for name, r in res.items():
        print(f"{name:12} {r['mean_profit']:8.2f} {r['p10']:8.2f} {r['median']:8.2f} {r['p90']:8.2f} "
              f"{r['win_rate']*100:5.0f} {r['mean_spend']:7.2f} {r['mean_earned']:7.2f} {r['good_products']:6.1f}")


if __name__ == "__main__":
    main()
