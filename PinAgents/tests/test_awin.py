import csv
import gzip
import io
import sys

import pytest

from pinagents.autopilot import _ad_link, run_cycle
from pinagents.dataflows import awin
from pinagents.dataflows.products import load_products
from tests.test_autopilot import FakeClient, full_config, state

FEED_LIST = ("Advertiser ID,Advertiser Name,Primary Region,Membership Status,Feed ID,Feed Name,Language,"
             "Datafeed Format,URL\n"
             "501,Desk Co,US,active,F9001,Desks,English,Awin,\n"
             "502,Google Only,US,active,F9002,G,English,Google,https://x\n"
             "503,Not Joined,US,not joined,F9003,N,English,Awin,\n")


def gz_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=awin.COLUMNS.split(","))
    w.writeheader()
    w.writerows(rows)
    return gzip.compress(buf.getvalue().encode())


FEED = gz_csv([
    {"aw_deep_link": "https://www.awin1.com/pclick.php?p=1&a=7&m=501", "product_name": "Standing Desk Frame",
     "aw_product_id": "1", "search_price": "249.00", "currency": "USD", "merchant_id": "501",
     "merchant_name": "Desk Co", "aw_image_url": "https://img/1.jpg", "in_stock": "1"},
    {"aw_deep_link": "https://www.awin1.com/pclick.php?p=2&a=7&m=501", "product_name": "Garden Hose",
     "aw_product_id": "2", "search_price": "30", "currency": "USD", "merchant_id": "501",
     "merchant_name": "Desk Co", "aw_image_url": "", "in_stock": "1"},
    {"aw_deep_link": "https://www.awin1.com/pclick.php?p=3&a=7&m=501", "product_name": "Desk Lamp EU",
     "aw_product_id": "3", "search_price": "40", "currency": "EUR", "merchant_id": "501",
     "merchant_name": "Desk Co", "aw_image_url": "", "in_stock": "1"},
])

TRANSACTIONS = [
    {"clickRefs": {"clickRef": "aw1-ad"}, "transactionDate": "2026-10-20T10:00:00", "clickDate": "2026-10-18T09:00:00",
     "commissionAmount": {"amount": 24.9}, "commissionStatus": "pending"},
    {"clickRefs": {"clickRef": "aw1"}, "transactionDate": "2026-10-21T10:00:00", "clickDate": "2026-10-21T08:00:00",
     "commissionAmount": {"amount": 5.0}, "commissionStatus": "declined"},
    {"clickRefs": {}, "transactionDate": "2026-10-21T10:00:00", "commissionAmount": {"amount": 1.0}},
]


class Resp:
    def __init__(self, body=None, text="", raw=b""):
        self.body, self.text, self.raw = body, text, io.BytesIO(raw)

    def json(self):
        return self.body

    def close(self):
        pass


@pytest.fixture
def fake_awin(monkeypatch):
    monkeypatch.setenv("AWIN_API_TOKEN", "t")
    monkeypatch.setenv("AWIN_PUBLISHER_ID", "7")
    monkeypatch.setenv("AWIN_FEED_KEY", "abc123")
    monkeypatch.setattr(awin.time, "sleep", lambda s: None)   # rate-limit pacing
    calls = []

    def request(method, url, **kw):
        calls.append((url, kw.get("params")))
        if url.endswith("/feedList"):
            return Resp(text=FEED_LIST)
        if "/fid/" in url:
            return Resp(raw=FEED)
        if url.endswith("/programmes"):
            if kw["params"]["relationship"] == "joined":
                return Resp(body=[{"id": 501, "name": "Desk Co"}])
            return Resp(body=[{"id": 601, "name": "Office Chairs Inc", "displayUrl": "https://chairs.example",
                               "description": "ergonomic office chairs"},
                              {"id": 602, "name": "Garden World", "description": "hoses"}])
        if url.endswith("/programmedetails"):
            return Resp(body={"commissionRange": [{"min": 5, "max": 10, "type": "percentage"},
                                                  {"min": 2, "max": 3, "type": "fix"}]})
        if url.endswith("/transactions/"):
            return Resp(body=sys.modules[__name__].TRANSACTIONS)
        raise AssertionError(url)
    monkeypatch.setattr(awin.http, "request", request)
    return calls


def test_discover_keeps_matching_usd_products_from_joined_feeds(fake_awin, tmp_path):
    products = load_products(awin.discover("standing desk", str(tmp_path / "p.csv")))
    assert [p["id"] for p in products] == ["aw1"]          # hose doesn't match, EUR lamp dropped
    p = products[0]
    assert p["url"].endswith("&clickref=aw1") and p["commission_rate"] == 0.10 and p["price"] == 249.0
    assert p["image_url"] == "https://img/1.jpg"
    assert not any("/fid/9002/" in url or "/fid/9003/" in url for url, _ in fake_awin)  # Google, not joined


def test_earnings_from_transactions_skip_declined(fake_awin, tmp_path):
    path = tmp_path / "e.csv"
    awin.write_earnings(str(path), "2026-10-01", "2026-10-26")
    rows = list(csv.DictReader(open(path)))
    assert rows == [{"product_id": "aw1-ad", "date": "2026-10-20", "earnings": "24.9", "click_date": "2026-10-18"}]
    windows = [p for url, p in fake_awin if url.endswith("/transactions/")]
    assert windows[0]["startDate"] == "2026-10-01T00:00:00" and windows[0]["endDate"] == "2026-10-27T00:00:00"


def test_join_list_ranks_fitting_unjoined_programmes(fake_awin):
    lines = awin.join_list_lines("office chairs")
    assert "| [Office Chairs Inc](https://chairs.example) | 601 | 10% |" in lines
    assert not any("Garden World" in line for line in lines)


def test_ad_links_tag_awin_clickref():
    assert _ad_link("https://www.awin1.com/pclick.php?p=1&clickref=aw1", "aw1").endswith("clickref=aw1-ad")


def test_autopilot_runs_on_awin_alone(fake_awin, tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "earnings.csv")
    run_cycle("standing desk", None, c, client, today="2026-10-25")
    run_cycle("standing desk", None, c, client, today="2026-10-26")
    st = state(tmp_path)
    assert st["groups"]["aw1"]["status"] == "ACTIVE" and st["groups"]["aw1"]["ad_tagged"]
    assert "aw1-ad" in (tmp_path / "earnings.csv").read_text()
    assert "Office Chairs Inc" not in (tmp_path / "results" / "join_list.md").read_text()  # no word shared


def test_both_networks_merge_products_and_earnings(fake_awin, monkeypatch, tmp_path):
    from pinagents.dataflows import cj
    from tests import test_cj
    monkeypatch.setenv("CJ_API_TOKEN", "t")
    monkeypatch.setenv("CJ_COMPANY_ID", "c")
    monkeypatch.setenv("CJ_WEBSITE_ID", "w")
    awin_request = awin.http.request

    def request(method, url, **kw):
        if "cj.com" in url:
            if method == "GET":
                return test_cj.Resp(content=test_cj.LOOKUP_XML)
            return test_cj.Resp(body=test_cj.COMMISSIONS if url == cj.COMMISSIONS else test_cj.PRODUCTS)
        return awin_request(method, url, **kw)
    monkeypatch.setattr(awin.http, "request", request)
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["network_earnings_path"] = str(tmp_path / "earnings.csv")
    log = run_cycle("desk rug", None, c, client, today="2026-10-25")
    assert any("Found products on CJ" in line for line in log) and any("on Awin" in line for line in log)
    assert {"cj9001", "aw1"} <= set(state(tmp_path)["products"])
    earnings = (tmp_path / "earnings.csv").read_text()
    assert "cj9001" in earnings and "aw1-ad" in earnings
    join = (tmp_path / "results" / "join_list.md").read_text()
    assert "CJ programs worth joining" in join and "Awin programmes" in join


def test_deleted_and_non_usd_transactions_are_left_out(fake_awin, monkeypatch, tmp_path):
    extra = [{"clickRefs": {"clickRef": "aw2"}, "transactionDate": "2026-10-22T10:00:00",
              "commissionAmount": {"amount": 9.0, "currency": "USD"}, "commissionStatus": "deleted"},
             {"clickRefs": {"clickRef": "aw3"}, "transactionDate": "2026-10-22T10:00:00",
              "commissionAmount": {"amount": 7.0, "currency": "GBP"}, "commissionStatus": "approved"}]
    monkeypatch.setattr(sys.modules[__name__], "TRANSACTIONS", TRANSACTIONS + extra)
    skipped = awin.write_earnings(str(tmp_path / "e.csv"), "2026-10-20", "2026-10-26")
    text = (tmp_path / "e.csv").read_text()
    assert skipped == 1 and "aw2" not in text and "aw3" not in text and "aw1-ad" in text


def test_a_broken_feed_is_skipped_and_unmatched_products_are_a_fallback(fake_awin, monkeypatch, tmp_path):
    feeds = FEED_LIST + "504,Broken Co,US,active,F9004,B,English,Awin,\n"
    real = awin.http.request

    def request(method, url, **kw):
        if url.endswith("/feedList"):
            return Resp(text=feeds)
        if "/fid/9004/" in url:
            return Resp(raw=b"<html>not gzip</html>")
        return real(method, url, **kw)
    monkeypatch.setattr(awin.http, "request", request)
    products = load_products(awin.discover("ergonomic chairs", str(tmp_path / "p.csv")))
    assert {p["id"] for p in products} == {"aw1", "aw2"}     # nothing matched: the feed's USD products


def test_awin_api_calls_are_paced_for_its_rate_limit(fake_awin, monkeypatch):
    waits = []
    monkeypatch.setattr(awin.time, "sleep", waits.append)
    awin.programmes("joined")
    awin.programmes("joined")
    assert waits and waits[-1] > 3


def test_products_survive_one_failed_commission_lookup(fake_awin, monkeypatch, tmp_path):
    feeds = FEED_LIST + "501,Desk Co,US,active,F9005,Desks 2,English,Awin,\n"
    real = awin.http.request
    lookups = []

    def request(method, url, **kw):
        if url.endswith("/feedList"):
            return Resp(text=feeds)
        if url.endswith("/programmedetails"):
            lookups.append(1)
            if len(lookups) == 1:
                raise awin.http.requests.Timeout("timed out")
        return real(method, url, **kw)
    monkeypatch.setattr(awin.http, "request", request)
    products = load_products(awin.discover("standing desk", str(tmp_path / "p.csv")))
    assert len(lookups) == 2 and [p["id"] for p in products] == ["aw1"]   # first feed's product kept
