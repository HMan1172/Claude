import pytest
import requests

from pinagents.dataflows import http
from pinagents.dataflows.pinterest_api import PinterestClient


class Resp:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(http.time, "sleep", lambda s: log.append(("sleep", s)))
    return log


def fake(monkeypatch, calls, statuses):
    def request(method, url, **kw):
        calls.append(("call", method))
        return Resp(statuses.pop(0))
    monkeypatch.setattr(http.requests, "request", request)


def test_reads_retry_on_server_errors(monkeypatch, calls):
    fake(monkeypatch, calls, [503, 500, 200])
    assert http.request("GET", "https://x").status_code == 200
    assert [c for c in calls if c[0] == "call"] == [("call", "GET")] * 3


def test_creates_do_not_retry_server_errors_but_do_retry_rate_limits(monkeypatch, calls):
    fake(monkeypatch, calls, [500])
    with pytest.raises(requests.HTTPError):
        http.request("POST", "https://x", idempotent=False)
    calls.clear()
    fake(monkeypatch, calls, [429, 200])
    assert http.request("POST", "https://x", idempotent=False).status_code == 200


def test_gives_up_after_the_last_retry(monkeypatch, calls):
    fake(monkeypatch, calls, [503, 503, 503, 503])
    with pytest.raises(requests.HTTPError):
        http.request("GET", "https://x")


def test_ad_ids_are_sent_as_repeated_params(monkeypatch):
    client = PinterestClient({"allow_ads": True, "pinterest_ad_account_id": "1"})
    client.token = "t"
    seen = {}
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: seen.update(kw["params"]) or [])
    client.ad_stats(["11", "22"], "2026-10-01", "2026-10-27")
    url = requests.Request("GET", "https://x", params=seen).prepare().url
    assert "ad_ids=11&ad_ids=22" in url


def test_bid_floor_in_dollars(monkeypatch):
    client = PinterestClient({"allow_ads": True, "pinterest_ad_account_id": "1", "ads_countries": ["US"]})
    client.token = "t"
    monkeypatch.setattr(client, "_request", lambda *a, **kw: {"bid_floors": [100000, 150000]})
    assert client.bid_floor() == 0.15
