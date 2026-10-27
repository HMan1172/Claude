import csv

import pytest

from pinagents.autopilot import run_cycle
from pinagents.dataflows import cj
from pinagents.dataflows.products import load_products
from tests.test_autopilot import FakeClient, full_config, state

LOOKUP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<cj-api><advertisers total-matched="2" records-returned="2" page-number="1">
 <advertiser>
  <advertiser-id>111</advertiser-id><advertiser-name>Rug Co</advertiser-name>
  <three-month-epc>45.00</three-month-epc><seven-day-epc>50.00</seven-day-epc>
  <relationship-status>joined</relationship-status>
  <primary-category><parent>Home &amp; Garden</parent><child>Rugs</child></primary-category>
  <actions><action><name>Online Sale</name><type>advanced sale</type><id>1</id>
   <commission><default type="item-level">12.00%</default></commission></action></actions>
 </advertiser>
 <advertiser>
  <advertiser-id>222</advertiser-id><advertiser-name>Leads Only</advertiser-name>
  <three-month-epc>N/A</three-month-epc><seven-day-epc>N/A</seven-day-epc>
  <actions><action><name>Signup</name><type>lead</type><id>2</id>
   <commission><default>USD 5.00</default></commission></action></actions>
 </advertiser>
</advertisers></cj-api>"""

PRODUCTS = {"data": {"shoppingProducts": {"resultList": [
    {"id": "9001", "title": "Washable Runner Rug", "imageLink": "https://img.rugco.com/runner.jpg",
     "price": {"amount": "129.00", "currency": "USD"},
     "linkCode": {"clickUrl": "https://www.anrdoezrs.net/click-123-456?url=https%3A%2F%2Frugco.com%2Frunner"}},
    {"id": "9002", "title": "Insecure Link Rug", "price": {"amount": "99.00", "currency": "USD"},
     "linkCode": {"clickUrl": "http://www.anrdoezrs.net/click-123-789"}},
]}}}

COMMISSIONS = {"data": {"publisherCommissions": {"payloadComplete": True, "records": [
    {"postingDate": "2026-10-20T10:00:00Z", "shopperId": "cj9001", "pubCommissionAmountUsd": 15.48},
    {"postingDate": "2026-10-21T10:00:00Z", "shopperId": None, "pubCommissionAmountUsd": 3.00},
]}}}


class Resp:
    def __init__(self, content=b"", body=None):
        self.content, self.body = content, body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


@pytest.fixture
def fake_cj(monkeypatch):
    monkeypatch.setenv("CJ_API_TOKEN", "t")
    monkeypatch.setenv("CJ_COMPANY_ID", "c")
    monkeypatch.setenv("CJ_WEBSITE_ID", "w")
    monkeypatch.setattr(cj.http, "request", lambda method, url, **kw: Resp(content=LOOKUP_XML) if method == "GET"
                        else Resp(body=COMMISSIONS if url == cj.COMMISSIONS else PRODUCTS))


def test_joined_programs_reads_sale_commission_and_epc(fake_cj):
    programs = cj.joined_programs()
    assert programs == [{"id": "111", "name": "Rug Co", "commission_rate": 0.12, "epc": 0.45,
                         "category": "Rugs", "site": ""}]


def test_discover_writes_a_products_csv(fake_cj, tmp_path):
    path, niche, categories = cj.discover("home decor", str(tmp_path / "p.csv"))
    assert niche == "home decor" and categories == ["Rugs"]
    products = load_products(path)
    assert [p["id"] for p in products] == ["cj9001"]   # the http link was dropped
    p = products[0]
    assert p["url"].endswith("&sid=cj9001") and p["price"] == 129.0
    assert p["commission_rate"] == 0.12 and p["epc"] == 0.45
    assert p["image_url"] == "https://img.rugco.com/runner.jpg"


def test_write_earnings_matches_products_by_sid(fake_cj, tmp_path):
    path = tmp_path / "earnings.csv"
    cj.write_earnings(str(path), "2026-10-15", "2026-10-26")
    rows = list(csv.DictReader(open(path)))
    assert {"product_id": "cj9001", "date": "2026-10-20", "earnings": "15.48", "click_date": ""} in rows
    assert all(r["product_id"] for r in rows)


def test_autopilot_runs_without_a_products_csv(fake_cj, tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "cj_earnings.csv")
    run_cycle("home decor", None, c, client, today="2026-10-25")
    run_cycle("home decor", None, c, client, today="2026-10-26")
    group = state(tmp_path)["groups"]["cj9001"]
    assert group["status"] == "ACTIVE" and group["bid"] == 0.18   # $0.45 EPC x 0.4
    assert (tmp_path / "cj_earnings.csv").exists()


def test_ads_track_their_own_sales(fake_cj, tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "cj_earnings.csv")
    run_cycle("home decor", None, c, client, today="2026-10-25")
    run_cycle("home decor", None, c, client, today="2026-10-26")
    ad = [x for x in client.calls if x[0] == "ad"][0]
    assert ad[3].endswith("&sid=cj9001-ad")                 # ad link differs from the pin's
    # The fake report has a $15.48 free-pin sale for cj9001 and nothing tagged -ad, so after the
    # test budget the ads must stop rather than take credit for the free pin's sale.
    st = state(tmp_path)
    for ad_id in st["ads"].values():
        client.stats[ad_id] = {"spend": 10.0, "clicks": 40, "impressions": 4000}
    run_cycle("home decor", None, c, client, today="2026-10-27")
    group = state(tmp_path)["groups"]["cj9001"]
    assert group["status"] == "PAUSED" and "without proving" in group["reason"]


def test_bids_drop_when_ads_earn_less_than_network_epc(fake_cj, tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "cj_earnings.csv")
    run_cycle("home decor", None, c, client, today="2026-10-25")
    run_cycle("home decor", None, c, client, today="2026-10-26")
    for ad_id in state(tmp_path)["ads"].values():   # 400 clicks, cheap enough to stay under test budget
        client.stats[ad_id] = {"spend": 4.0, "clicks": 200, "impressions": 9000}
    run_cycle("home decor", None, c, client, today="2026-10-27")
    st = state(tmp_path)
    # Predicted 400 x $0.45 = $180, earned $0 on ads; prior is 5 sales x $15.48 commission.
    assert st["epc_factor"] == pytest.approx(77.4 / 257.4, rel=1e-3)
    assert st["groups"]["cj9001"]["bid"] == c["ads_min_bid"]   # 0.18 x 0.30 = 0.05, held at the minimum
    assert ("bid", st["groups"]["cj9001"]["id"], c["ads_min_bid"]) in client.calls


def test_autopilot_picks_its_own_niche(fake_cj, tmp_path, monkeypatch):
    queries = []
    real = cj.http.request
    monkeypatch.setattr(cj.http, "request", lambda method, url, **kw: (
        queries.append(kw["json"]["query"]) if method == "POST" else None) or real(method, url, **kw))
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "cj_earnings.csv")
    log = run_cycle(None, None, c, client, today="2026-10-25")
    assert state(tmp_path)["niche"] == "Rugs"
    assert any("'Rugs'" in line for line in log)
    assert "keywords" not in next(q for q in queries if "shoppingProducts" in q)  # whole catalog


def test_check_command_reports_each_service(fake_cj, monkeypatch, capsys):
    from cli.main import main
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PINTEREST_REFRESH_TOKEN", raising=False)
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "OK    CJ programs: 1 joined" in out and "Rug Co: 12%" in out
    assert "OK    CJ products: 2 products" in out
    assert "OK    CJ commissions:" in out


def test_rejected_field_names_come_back_with_the_real_ones(fake_cj, monkeypatch):
    def post(method, url, json, **kw):
        if "__type" in json["query"]:
            return Resp(body={"data": {"__type": {"fields": [{"name": "clickURL", "args": []},
                                                             {"name": "imageURL", "args": []}]}}})
        return Resp(body={"errors": [{"message": 'Cannot query field "clickUrl" on type "LinkCode".'}]})
    monkeypatch.setattr(cj.http, "request", post)
    with pytest.raises(ValueError, match="LinkCode has: clickURL, imageURL"):
        cj.program_products({"id": "111"}, [])


def test_delivery_estimate_converts_micro_currency(monkeypatch):
    from pinagents.dataflows.pinterest_api import PinterestClient
    client = PinterestClient({"allow_ads": True, "pinterest_ad_account_id": "1", "ads_countries": ["US"]})
    client.token = "t"
    sent = {}
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: sent.update(kw) or {
        "derived_metrics": {"cpc": 420000, "cpc_lower": 250000, "cpc_upper": 900000, "weekly_click": 85}})
    est = client.delivery_estimate(5.0, "2026-10-27")
    assert est == {"cpc": 0.42, "cpc_lower": 0.25, "cpc_upper": 0.9, "weekly_click": 85}
    assert sent["json"][0]["daily_spend_cap"] == 5_000_000


def test_products_still_load_if_cj_has_no_imagelink_field(fake_cj, monkeypatch):
    def post(method, url, json, **kw):
        if "__type" in json["query"]:
            return Resp(body={"data": {"__type": None}})
        if "imageLink" in json["query"]:
            return Resp(body={"errors": [{"message": 'Cannot query field "imageLink" on type "ShoppingProduct".'}]})
        return Resp(body=PRODUCTS)
    monkeypatch.setattr(cj.http, "request", post)
    assert len(cj.program_products({"id": "111"}, [])) == 2


def test_niche_skips_categories_nobody_searches_for():
    programs = [{"category": "Rugs", "epc": 0.45, "commission_rate": 0.12},
                {"category": "Lamps", "epc": 0.30, "commission_rate": 0.10}]
    assert cj.pick_niche(programs) == "Rugs"
    assert cj.pick_niche(programs, lambda cats: {"Rugs": 200, "Lamps": 50000}) == "Lamps"
    assert cj.pick_niche(programs, lambda cats: {"Rugs": 8000, "Lamps": 50000}) == "Rugs"  # within 10% of top
    assert cj.pick_niche(programs, lambda cats: {}) == "Rugs"           # unknown demand: EPC decides


def test_search_volume_parsing():
    from pinagents.dataflows.pinterest_api import _volume
    assert _volume("12,500") == 12500 and _volume("10K-100K") == 10000
    assert _volume("1.5M") == 1.5e6 and _volume(None) is None and _volume("n/a") is None


def test_check_shows_what_cj_returned_when_nothing_is_readable(fake_cj, monkeypatch, capsys):
    from cli.main import main
    odd = b"<cj-api><advertisers><advertiser><id>1</id><program-name>X</program-name></advertiser></advertisers></cj-api>"
    real = cj.http.request
    monkeypatch.setattr(cj.http, "request", lambda method, url, **kw: Resp(content=odd) if method == "GET"
                        else real(method, url, **kw))
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PINTEREST_REFRESH_TOKEN", raising=False)
    assert main(["check"]) == 1
    assert "first <advertiser> has: ['id', 'program-name']" in capsys.readouterr().out


def test_earnings_survive_cj_rejecting_click_dates(fake_cj, monkeypatch, tmp_path):
    def post(method, url, json, **kw):
        if "__type" in json["query"]:
            return Resp(body={"data": {"__type": None}})
        if "clickDate" in json["query"]:
            return Resp(body={"errors": [{"message": 'Cannot query field "clickDate" on type "Commission".'}]})
        return Resp(body=COMMISSIONS)
    monkeypatch.setattr(cj.http, "request", post)
    cj.write_earnings(str(tmp_path / "e.csv"), "2026-10-20", "2026-10-26")
    assert "cj9001,2026-10-20,15.48," in (tmp_path / "e.csv").read_text()


def test_join_list_ranks_unjoined_programs_in_the_niche_first(fake_cj, monkeypatch, tmp_path):
    notjoined = b"""<cj-api><advertisers>
     <advertiser><advertiser-id>7</advertiser-id><advertiser-name>Big Lamps</advertiser-name>
      <program-url>https://biglamps.example</program-url><three-month-epc>90.00</three-month-epc>
      <primary-category><child>Lighting</child></primary-category>
      <actions><action><type>sale</type><commission><default>8%</default></commission></action></actions></advertiser>
     <advertiser><advertiser-id>8</advertiser-id><advertiser-name>Rug Outlet</advertiser-name>
      <three-month-epc>30.00</three-month-epc><primary-category><child>Rugs</child></primary-category>
      <actions><action><type>sale</type><commission><default>15%</default></commission></action></actions></advertiser>
    </advertisers></cj-api>"""
    seen = []
    monkeypatch.setattr(cj.http, "request", lambda method, url, **kw: seen.append(kw["params"]["advertiser-ids"]) or Resp(content=notjoined))
    assert cj.write_join_list("Rugs", str(tmp_path / "join.md")) == 2
    text = (tmp_path / "join.md").read_text()
    assert seen == ["notjoined"]
    assert text.index("Rug Outlet") < text.index("Big Lamps")          # niche fit beats higher EPC
    assert "| [Big Lamps](https://biglamps.example) | 7 | Lighting | 8% | $0.90 |" in text


def test_autopilot_writes_the_join_list_weekly(fake_cj, tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "cj_earnings.csv")
    log = run_cycle("Rugs", None, c, client, today="2026-10-25")
    assert any("worth joining" in line for line in log)
    assert not any("worth joining" in line for line in run_cycle("Rugs", None, c, client, today="2026-10-28"))
    assert any("worth joining" in line for line in run_cycle("Rugs", None, c, client, today="2026-11-01"))


def test_join_list_counts_categories_that_supplied_products_as_the_niche(fake_cj, monkeypatch, tmp_path):
    notjoined = b"""<cj-api><advertisers>
     <advertiser><advertiser-id>7</advertiser-id><advertiser-name>Big Lamps</advertiser-name>
      <three-month-epc>90.00</three-month-epc><primary-category><child>Lighting</child></primary-category>
      <actions><action><type>sale</type><commission><default>8%</default></commission></action></actions></advertiser>
     <advertiser><advertiser-id>9</advertiser-id><advertiser-name>Desk Depot</advertiser-name>
      <three-month-epc>20.00</three-month-epc><primary-category><child>Furniture</child></primary-category>
      <actions><action><type>sale</type><commission><default>10%</default></commission></action></actions></advertiser>
    </advertisers></cj-api>"""
    monkeypatch.setattr(cj.http, "request", lambda method, url, **kw: Resp(content=notjoined))
    cj.write_join_list("home office", str(tmp_path / "join.md"), categories=["Furniture"])
    text = (tmp_path / "join.md").read_text()
    assert text.index("Desk Depot") < text.index("Big Lamps")   # "home office" shares no word with "Furniture"
