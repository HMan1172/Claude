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
    {"id": "9001", "title": "Washable Runner Rug", "price": {"amount": "129.00", "currency": "USD"},
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
    monkeypatch.setattr(cj.requests, "get", lambda url, **kw: Resp(content=LOOKUP_XML))
    monkeypatch.setattr(cj.requests, "post", lambda url, **kw: Resp(
        body=COMMISSIONS if url == cj.COMMISSIONS else PRODUCTS))


def test_joined_programs_reads_sale_commission_and_epc(fake_cj):
    programs = cj.joined_programs()
    assert programs == [{"id": "111", "name": "Rug Co", "commission_rate": 0.12, "epc": 0.45}]


def test_discover_writes_a_products_csv(fake_cj, tmp_path):
    path = cj.discover("home decor", str(tmp_path / "p.csv"))
    products = load_products(path)
    assert [p["id"] for p in products] == ["cj9001"]   # the http link was dropped
    p = products[0]
    assert p["url"].endswith("&sid=cj9001") and p["price"] == 129.0
    assert p["commission_rate"] == 0.12 and p["epc"] == 0.45


def test_write_earnings_matches_products_by_sid(fake_cj, tmp_path):
    path = tmp_path / "earnings.csv"
    cj.write_earnings(str(path), "2026-10-15", "2026-10-26")
    rows = list(csv.DictReader(open(path)))
    assert {"product_id": "cj9001", "date": "2026-10-20", "earnings": "15.48"} in rows
    assert all(r["product_id"] for r in rows)


def test_autopilot_runs_without_a_products_csv(fake_cj, tmp_path):
    c, client = full_config(tmp_path, earnings_csv=""), FakeClient()
    c["cj_earnings_path"] = str(tmp_path / "cj_earnings.csv")
    run_cycle("home decor", None, c, client, today="2026-10-25")
    run_cycle("home decor", None, c, client, today="2026-10-26")
    group = state(tmp_path)["groups"]["cj9001"]
    assert group["status"] == "ACTIVE" and group["bid"] == 0.18   # $0.45 EPC x 0.4
    assert (tmp_path / "cj_earnings.csv").exists()
