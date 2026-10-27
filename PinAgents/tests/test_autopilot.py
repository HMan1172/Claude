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

    def create_ad(self, ad_group_id, pin_id, name, destination_url=None):
        self.calls.append(("ad", ad_group_id, pin_id, destination_url))
        return self._id("ad")

    def bid_floor(self):
        return None

    def trending_keywords(self, niche):
        return getattr(self, "trends", [])

    def keyword_volumes(self, keywords):
        return getattr(self, "volumes", {})

    def update_ad_group(self, ad_group_id, max_cpc=None, daily_budget=None):
        if max_cpc is not None:
            self.calls.append(("bid", ad_group_id, max_cpc))
        if daily_budget is not None:
            self.calls.append(("budget", ad_group_id, daily_budget))

    def update_campaign_cap(self, campaign_id, lifetime_cap):
        self.calls.append(("cap", campaign_id, lifetime_cap))

    def set_status(self, kind, ids, status):
        self.calls.append(("status", kind, tuple(ids), status))

    def ad_stats(self, ad_ids, start, end):
        self.calls.append(("stats", end))
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


def test_products_are_judged_on_spend_old_enough_for_sales_to_post(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    ends = [x[1] for x in client.calls if x[0] == "stats"]
    assert "2026-10-26" in ends and "2026-10-22" in ends


def test_each_pin_is_saved_before_the_next_is_posted(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    seen = []
    real = client.create_pin

    def create_pin(pin, image_path):
        on_disk = json.loads((tmp_path / "autopilot.json").read_text())["posted"]
        seen.append(len(on_disk))
        return real(pin, image_path)
    client.create_pin = create_pin
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    assert seen == [0, 1, 2]   # a crash mid-cycle can't make the program forget a posted pin


def test_a_batch_with_no_approved_pins_waits_before_retrying(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("id,name,url,price,commission_rate\nb,Short Link,https://bit.ly/abc,20,5%\n")
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("test", str(bad), c, client, today="2026-10-25")
    assert state(tmp_path)["empty_batch"] == "2026-10-25"
    log = run_cycle("test", str(bad), c, client, today="2026-10-26")
    assert any("next try after" in line for line in log)
    log = run_cycle("test", str(bad), c, client, today="2026-10-28")
    assert any("Agents wrote 0 pins" in line for line in log)


def test_a_refused_ad_group_does_not_stop_other_products(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    real = client.create_ad_group

    def create_ad_group(campaign_id, name, daily_budget, max_cpc):
        if len([x for x in client.calls if x[0] == "group"]) == 0:
            client.calls.append(("group", name, daily_budget, max_cpc))
            raise ValueError("Pinterest refused the ad_group: budget below minimum")
        return real(campaign_id, name, daily_budget, max_cpc)
    client.create_ad_group = create_ad_group
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    log = run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    groups = state(tmp_path)["groups"]
    assert sorted(g["status"] for g in groups.values()) == ["ACTIVE", "REFUSED"]
    assert any("budget below minimum" in line for line in log)


def test_check_tests_the_ai_provider(monkeypatch, capsys):
    from cli.main import main
    from pinagents import llm
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PINTEREST_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("CJ_API_TOKEN", raising=False)
    monkeypatch.setattr(llm.LLM, "_call", lambda self, system, user, model: '{"ok": true}')
    assert main(["check", "--provider", "anthropic"]) == 0
    assert "OK    AI provider (quick model claude-haiku-5-5): replied" in capsys.readouterr().out


def test_refused_ad_groups_are_retried_after_a_cool_off_up_to_three_tries(tmp_path):
    c, client = full_config(tmp_path, ads_daily_cap=50.0), FakeClient()
    tries = []

    def refuse(campaign_id, name, daily_budget, max_cpc):
        tries.append(name)
        raise ValueError("account under review")
    client.create_ad_group = refuse
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")   # first tries
    run_cycle("home office", PRODUCTS, c, client, today="2026-11-01")   # newly posted products only
    assert max(tries.count(name) for name in tries) == 1                 # no retry while cooling off
    for day in ["2026-11-09", "2026-11-23", "2026-12-07", "2026-12-21"]:
        run_cycle("home office", PRODUCTS, c, client, today=day)
    per_product = {name: tries.count(name) for name in tries}
    assert set(per_product.values()) == {3}                              # gave up after 3 tries


def test_trend_analyst_sees_real_pinterest_trends(tmp_path, monkeypatch):
    from pinagents import llm
    prompts = []
    real = llm.LLM.json
    monkeypatch.setattr(llm.LLM, "json", lambda self, system, user, **kw: prompts.append(user) or real(self, system, user, **kw))
    c, client = full_config(tmp_path), FakeClient()
    client.trends = [{"keyword": "home office ideas", "trend": "seasonal", "growth_month": 140, "growth_year": 35}]
    log = run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    assert any("Pinterest reports 1 trending searches" in line for line in log)
    assert any("home office ideas" in p and "Real Pinterest search trends" in p for p in prompts)


def test_trending_keywords_calls_pinterest_with_niche_words(monkeypatch):
    from pinagents.dataflows.pinterest_api import PinterestClient
    client = PinterestClient({"ads_countries": ["GB"]})
    client.token = "t"
    calls = []
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: calls.append((path, kw["params"])) or {
        "trends": [{"keyword": "desk setup", "pct_growth_mom": 80, "pct_growth_yoy": 20}]})
    out = client.trending_keywords("Home Office & Desk")
    assert calls[0] == ("/trends/keywords/GB+IE/top/seasonal", {"include_keywords": ["home", "office", "desk"], "limit": 20})
    assert out == [{"keyword": "desk setup", "trend": "seasonal", "growth_month": 80, "growth_year": 20}]


def test_sales_delay_is_measured_once_there_are_enough_sales(tmp_path):
    from pinagents.autopilot import _sales_delay
    path = tmp_path / "earnings.csv"
    rows = ["product_id,date,earnings,click_date"] + [f"a,2026-10-{10 + d:02d},5,2026-10-10" for d in [1] * 8 + [6, 9]]
    path.write_text("\n".join(rows[:6]))
    assert _sales_delay(str(path)) == 4                       # 5 sales: too few, default
    path.write_text("\n".join(rows))
    assert _sales_delay(str(path)) == 6                       # 90% of 10 sales posted within 6 days


def test_sales_delay_prefers_ad_sales_once_there_are_enough(tmp_path):
    from pinagents.autopilot import _sales_delay
    path = tmp_path / "earnings.csv"
    organic = [f"a,2026-10-20,5,2026-10-10" for _ in range(10)]          # 10-day lag
    ads = [f"a-ad,2026-10-12,5,2026-10-10" for _ in range(10)]           # 2-day lag
    path.write_text("\n".join(["product_id,date,earnings,click_date"] + organic + ads[:5]))
    assert _sales_delay(str(path)) == 10                     # only 5 ad sales: use all sales
    path.write_text("\n".join(["product_id,date,earnings,click_date"] + organic + ads))
    assert _sales_delay(str(path)) == 2                      # 10 ad sales: ads alone


def test_raised_caps_and_budgets_reach_pinterest(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    cid = state(tmp_path)["campaign"]["id"]
    c.update(ads_total_cap=500.0, ad_group_daily_budget=2.0)
    log = run_cycle("home office", PRODUCTS, c, client, today="2026-10-27")
    assert ("cap", cid, 500.0) in client.calls
    groups = [g for g in state(tmp_path)["groups"].values() if g["status"] == "ACTIVE"]
    assert groups and all(g["budget"] == 2.0 for g in groups)
    assert all(("budget", g["id"], 2.0) in client.calls for g in groups)
    assert any("total cap set to $500.00" in line for line in log)


def test_raised_budgets_stay_within_the_daily_cap(tmp_path):
    c, client = full_config(tmp_path), FakeClient()
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-25")
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-26")
    c.update(ad_group_daily_budget=20.0)                     # 2 products x $20 > $5 daily cap
    run_cycle("home office", PRODUCTS, c, client, today="2026-10-27")
    st = state(tmp_path)
    active = [g for g in st["groups"].values() if g["status"] == "ACTIVE"]
    assert sum(g["budget"] for g in active) <= c["ads_daily_cap"]
    assert st["campaign"]["status"] == "ACTIVE"
