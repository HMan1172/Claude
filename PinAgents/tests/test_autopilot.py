import json
import os

from pinagents.autopilot import run_cycle
from pinagents.dataflows.pinterest_api import PinterestClient

ROOT = os.path.dirname(os.path.dirname(__file__))
PRODUCTS = os.path.join(ROOT, "products_example.csv")


class FakeClient:
    """Stands in for Pinterest with posting and ads switched on; records every call."""
    live = ads_live = True

    def __init__(self):
        self.calls, self.stats, self.n = [], {}, 0

    def _id(self, kind):
        self.n += 1
        return f"{kind}{self.n}"

    def refresh_access_token(self):
        return False

    def create_pin(self, pin, image_path):
        assert os.path.exists(image_path)
        self.calls.append(("pin", pin["key"]))
        return {"id": self._id("pin")}

    def create_campaign(self, name, lifetime_cap):
        self.calls.append(("campaign", lifetime_cap))
        return self._id("c")

    def create_ad_group(self, campaign_id, name, daily_budget, max_cpc):
        self.calls.append(("group", name, daily_budget, max_cpc))
        return self._id("g")

    def create_ad(self, ad_group_id, pin_id, name):
        self.calls.append(("ad", ad_group_id, pin_id))
        return self._id("ad")

    def set_status(self, kind, ids, status):
        self.calls.append(("status", kind, tuple(ids), status))

    def ad_stats(self, ad_ids, start, end):
        return {i: self.stats[i] for i in ad_ids if i in self.stats}

    def campaign_daily_spend(self, campaign_id, start, end):
        return {}


def cfg(tmp_path, **kw):
    return {"llm_provider": "mock", "results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "lessons.md"),
            "autopilot_state_path": str(tmp_path / "autopilot.json"),
            "ads_ledger_path": str(tmp_path / "ledger.json"),
            "kill_switch_path": str(tmp_path / "STOP"),
            "earnings_csv": str(tmp_path / "earnings.csv"),
            "assumed_conversion_rate": 0.2, **kw}


def full_config(tmp_path, **kw):
    from pinagents.default_config import DEFAULT_CONFIG
    return {**DEFAULT_CONFIG, **cfg(tmp_path, **kw)}


def state(tmp_path):
    return json.loads((tmp_path / "autopilot.json").read_text())


def test_cycle_writes_posts_and_advertises(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    assert len(state(tmp_path)["queue"]) == 10   # written, first ones due tomorrow
    assert not [x for x in client.calls if x[0] == "pin"]

    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    st = state(tmp_path)
    assert len(st["posted"]) == 3 and len(st["queue"]) == 7
    assert ("campaign", c["ads_total_cap"]) in client.calls
    groups = [x for x in client.calls if x[0] == "group"]
    assert len(groups) == 2                      # two products among the first three pins
    assert all(x[3] >= c["ads_min_bid"] for x in groups)
    assert len(st["ads"]) == 3
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["days"]["2026-10-26"] == 2.0    # $1/day per product, authorized by BudgetGuard


def test_losing_product_is_paused_and_lessons_saved(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    st = state(tmp_path)
    loser = st["posted"][next(iter(st["ads"]))]["product_id"]
    for key, ad_id in st["ads"].items():
        if st["posted"][key]["product_id"] == loser:
            client.stats[ad_id] = {"spend": 6.0, "clicks": 30, "impressions": 2000}
    (tmp_path / "earnings.csv").write_text(f"product_id,date,earnings\n{loser},2026-10-26,2.00\n")

    log = run_cycle("home office", PRODUCTS, c, client, today="2026-10-27")
    st = state(tmp_path)
    assert st["groups"][loser]["status"] == "PAUSED"
    assert "losing money" in st["groups"][loser]["reason"]
    assert ("status", "ad_groups", (st["groups"][loser]["id"],), "PAUSED") in client.calls
    assert any("lessons" in line for line in log)
    assert "Totals" in (tmp_path / "lessons.md").read_text()


def test_unproven_product_stops_at_test_budget(tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    st = state(tmp_path)
    key, ad_id = next(iter(st["ads"].items()))
    client.stats[ad_id] = {"spend": 20.0, "clicks": 80, "impressions": 9000}
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-27")
    pid = st["posted"][key]["product_id"]
    assert state(tmp_path)["groups"][pid]["status"] == "PAUSED"


def test_kill_switch_and_caps_pause_campaign(tmp_path):
    c, client = full_config(tmp_path, ads_daily_cap=1.0), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    log = run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    assert len([x for x in client.calls if x[0] == "group"]) == 1   # daily cap fits one product
    assert any("No budget for new ads" in line for line in log)

    (tmp_path / "STOP").write_text("")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-27")
    cid = state(tmp_path)["campaign"]["id"]
    assert client.calls[-1] == ("status", "campaigns", (cid,), "PAUSED")


def test_unaffordable_products_are_never_advertised(tmp_path):
    # At a 2% conversion rate, a 4% commission on a $35 lamp earns ~3 cents a click.
    c, client = full_config(tmp_path, assumed_conversion_rate=0.02), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    assert not [x for x in client.calls if x[0] in ("campaign", "group", "ad")]
    assert all(g["status"] == "SKIPPED" for g in state(tmp_path)["groups"].values())


def test_dry_run_keeps_separate_state_and_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    c = full_config(tmp_path)
    client = PinterestClient(c)
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    log = run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    assert any("Dry run" in line for line in log)
    assert (tmp_path / "autopilot-dry.json").exists() and not (tmp_path / "autopilot.json").exists()
    assert (tmp_path / "ledger-dry.json").exists() and not (tmp_path / "ledger.json").exists()


def test_amazon_links_are_never_advertised(tmp_path):
    products = tmp_path / "products.csv"
    products.write_text("id,name,url,price,commission_rate\n"
                        "a,Lamp,https://www.amazon.com/dp/B0TEST?tag=me-20,500,10%\n"
                        "b,Rug,https://www.example.com/rug?aff=me,500,10%\n")
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", str(products), c, client, today="2026-10-25")
    run_cycle("home office", str(products), c, client, today="2026-10-26")
    groups = state(tmp_path)["groups"]
    assert groups["a"]["status"] == "SKIPPED" and "Amazon" in groups["a"]["reason"]
    assert groups["b"]["status"] == "ACTIVE"


def test_network_conversion_rate_sets_the_bid(tmp_path):
    products = tmp_path / "products.csv"
    products.write_text("id,name,url,price,commission_rate,conversion_rate\n"
                        "a,Rug,https://www.example.com/rug,100,10%,5%\n")
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home", str(products), c, client, today="2026-10-25")
    run_cycle("home", str(products), c, client, today="2026-10-26")
    assert state(tmp_path)["groups"]["a"]["bid"] == 0.2   # 5% x $10 commission x 0.4
