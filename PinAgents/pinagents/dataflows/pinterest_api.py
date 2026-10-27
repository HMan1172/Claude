"""Pinterest's official v5 API: posting pins (Stage 2) and running ads (Stage 3).

Posting needs a Business account, a developer app, and Standard access (Trial access is
mostly read-only). Ads also need an ad account with billing set up and the ads:write scope.
Until config["allow_posting"] / config["allow_ads"] are True, every write is a dry run that
returns a fake id, so the autopilot can be tested end to end without touching your account.
"""
import base64
import json
import os
import re
import time

import requests

from . import http

API = "https://api.pinterest.com/v5"
MICRO = 1_000_000
# Regions Pinterest's trends API covers; a country outside them uses US trends.
TREND_REGIONS = ["US", "CA", "DE", "FR", "ES", "IT", "AR", "BR", "CO", "MX", "DE+AT+CH", "GB+IE",
                 "IT+ES+PT+GR+MT", "PL+RO+HU+SK+CZ", "SE+DK+FI+NO", "NL+BE+LU", "AU+NZ"]


class PinterestClient:
    def __init__(self, config: dict):
        self.config = config
        self.token = os.getenv("PINTEREST_ACCESS_TOKEN", "")
        self.ad_account = config.get("pinterest_ad_account_id", "")
        self._dry_ids = 0

    @property
    def live(self) -> bool:
        return bool(self.config.get("allow_posting") and self.token)

    @property
    def ads_live(self) -> bool:
        return bool(self.config.get("allow_ads") and self.token and self.ad_account)

    # ---- auth -----------------------------------------------------------
    def refresh_access_token(self) -> bool:
        """Swap the refresh token for a new access token so unattended runs don't expire.

        Pinterest refresh tokens last 60 days and are replaced on every refresh, so the new one
        is saved to config["pinterest_token_path"] (readable only by you) for the next run.
        """
        app_id, secret = os.getenv("PINTEREST_APP_ID", ""), os.getenv("PINTEREST_APP_SECRET", "")
        path = self.config["pinterest_token_path"]
        refresh = os.getenv("PINTEREST_REFRESH_TOKEN", "")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                refresh = json.load(f).get("refresh_token") or refresh
        if not (app_id and secret and refresh):
            return False
        # Not retried after a server error: Pinterest may already have rotated the refresh token.
        resp = http.request("POST", f"{API}/oauth/token", idempotent=False, timeout=30, auth=(app_id, secret),
                            data={"grant_type": "refresh_token", "refresh_token": refresh})
        body = resp.json()
        self.token = body["access_token"]
        if body.get("refresh_token"):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"refresh_token": body["refresh_token"], "saved_at": int(time.time())}, f)
        return True

    def _request(self, method: str, path: str, idempotent: bool = True, **kw):
        return http.request(method, f"{API}{path}", idempotent=idempotent, timeout=30,
                            headers={"Authorization": f"Bearer {self.token}"}, **kw).json()

    def _dry_id(self, kind: str) -> str:
        self._dry_ids += 1
        return f"dry-{kind}-{self._dry_ids}"

    # ---- organic pins ---------------------------------------------------
    def create_pin(self, pin: dict, image_path: str, board_id: str = "") -> dict:
        board_id = board_id or self.config.get("pinterest_board_id", "")
        with open(image_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        body = {
            "board_id": board_id,
            "title": pin["title"],
            "description": pin["description"],
            "link": pin["link"],
            "alt_text": pin.get("alt_text", ""),
            "media_source": {"source_type": "image_base64", "content_type": "image/png", "data": data},
        }
        if not self.live:
            return {"dry_run": True, "id": self._dry_id("pin"),
                    "would_post": {k: v for k, v in body.items() if k != "media_source"}}
        if not board_id:
            raise ValueError("Set PINTEREST_BOARD_ID before posting.")
        return self._request("POST", "/pins", idempotent=False, json=body)

    # ---- ads ------------------------------------------------------------
    def _batch_create(self, kind: str, item: dict) -> str:
        if not self.ads_live:
            return self._dry_id(kind.rstrip("s"))
        out = self._request("POST", f"/ad_accounts/{self.ad_account}/{kind}", idempotent=False, json=[item])
        result = out["items"][0]
        if result.get("exceptions"):
            raise ValueError(f"Pinterest refused the {kind[:-1]}: {result['exceptions']}")
        return result["data"]["id"]

    def create_campaign(self, name: str, lifetime_cap: float) -> str:
        """Traffic campaign. lifetime_spend_cap is Pinterest's own backstop behind BudgetGuard."""
        return self._batch_create("campaigns", {
            "name": name, "status": "ACTIVE", "objective_type": "CONSIDERATION",
            "lifetime_spend_cap": int(lifetime_cap * MICRO),
        })

    def create_ad_group(self, campaign_id: str, name: str, daily_budget: float, max_cpc: float) -> str:
        """Pays per outbound click, never more than max_cpc, never more than daily_budget a day."""
        return self._batch_create("ad_groups", {
            "campaign_id": campaign_id, "name": name, "status": "ACTIVE",
            "billable_event": "CLICKTHROUGH", "budget_type": "DAILY",
            "budget_in_micro_currency": int(daily_budget * MICRO),
            "bid_in_micro_currency": int(max_cpc * MICRO),
            "auto_targeting_enabled": True,
            "targeting_spec": {"LOCATION": self.config["ads_countries"]},
        })

    def create_ad(self, ad_group_id: str, pin_id: str, name: str, destination_url: str | None = None) -> str:
        ad = {"ad_group_id": ad_group_id, "pin_id": pin_id, "creative_type": "REGULAR",
              "status": "ACTIVE", "name": name}
        if destination_url:
            ad["destination_url"] = destination_url
        return self._batch_create("ads", ad)

    def delivery_estimate(self, daily_budget: float, start_date: str) -> dict | None:
        """Pinterest's forecast for a traffic campaign with our targeting, before spending anything:
        {"cpc", "cpc_lower", "cpc_upper", "weekly_click"} in dollars. This endpoint is in beta and
        not open to every app, so None means "no estimate" and the caller decides without it."""
        if not self.ads_live:
            return None
        body = [{"objective_type": "CONSIDERATION", "budget_duration_type": "FIXED_DAILY",
                 "daily_spend_cap": int(daily_budget * MICRO), "start_date": start_date,
                 "ad_groups": [{"auto_targeting_enabled": True,
                                "targeting_spec": {"LOCATION": self.config["ads_countries"]}}]}]
        try:
            out = self._request("POST", f"/ad_accounts/{self.ad_account}/campaigns/delivery_estimates", json=body)
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code in (400, 401, 403, 404):
                return None
            raise
        m = out.get("derived_metrics") or {}
        if not m.get("cpc"):
            return None
        # The docs don't state the unit; budgets in this API are micro-currency, so treat
        # anything above $100 per click as micro-currency.
        scale = MICRO if m["cpc"] > 100 else 1
        est = {k: m[k] / scale for k in ("cpc", "cpc_lower", "cpc_upper") if m.get(k) is not None}
        est["weekly_click"] = m.get("weekly_click")
        return est

    def bid_floor(self) -> float | None:
        """Pinterest's minimum cost-per-click bid for our targeting, in dollars (None if unknown)."""
        if not self.ads_live:
            return None
        try:
            out = self._request("POST", f"/ad_accounts/{self.ad_account}/bid_floor", json={"bid_floor_specs": [{
                "billable_event": "CLICKTHROUGH", "currency": "USD", "objective_type": "CONSIDERATION",
                "countries": self.config["ads_countries"]}]})
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code in (400, 401, 403, 404):
                return None
            raise
        floors = out.get("bid_floors") or []
        return max(floors) / MICRO if floors else None

    def trending_keywords(self, niche: str) -> list[dict]:
        """Pinterest's top seasonal and growing searches containing any word of the niche, with their
        month-over-month and year-over-year growth in %. Read-only, so it runs in dry runs too;
        empty when there's no token or Pinterest refuses."""
        words = [w for w in re.findall(r"[a-z0-9]+", niche.lower()) if len(w) > 2][:50]
        if not self.token or not words:
            return []
        country = self.config["ads_countries"][0]
        region = next((r for r in TREND_REGIONS if country in r.split("+")), "US")
        trends = {}
        for trend_type in ("seasonal", "growing"):
            try:
                out = self._request("GET", f"/trends/keywords/{region}/top/{trend_type}",
                                    params={"include_keywords": words, "limit": 20})
            except requests.HTTPError as err:
                if err.response is not None and err.response.status_code in (400, 401, 403, 404):
                    return []
                raise
            for t in out.get("trends") or []:
                trends.setdefault(t["keyword"], {"keyword": t["keyword"], "trend": trend_type,
                                                 "growth_month": t.get("pct_growth_mom"),
                                                 "growth_year": t.get("pct_growth_yoy")})
        return list(trends.values())

    def keyword_volumes(self, keywords: list[str]) -> dict:
        """How often each keyword is searched on Pinterest in the first targeted country,
        {keyword: searches}. Empty when unknown (no ad account, dry run, or refused)."""
        if not self.ads_live or not keywords:
            return {}
        try:
            out = self._request("GET", f"/ad_accounts/{self.ad_account}/keywords/metrics", params={
                "country_code": self.config["ads_countries"][0], "keywords": keywords[:2000]})
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code in (400, 401, 403, 404):
                return {}
            raise
        volumes = {}
        for row in out.get("data") or []:
            v = _volume((row.get("metrics") or {}).get("keyword_query_volume"))
            if v is not None:
                volumes[row["keyword"]] = v
        return volumes

    def update_ad_group(self, ad_group_id: str, max_cpc: float | None = None,
                        daily_budget: float | None = None) -> None:
        if not self.ads_live or ad_group_id.startswith("dry-"):
            return
        change = {"id": ad_group_id}
        if max_cpc is not None:
            change["bid_in_micro_currency"] = int(max_cpc * MICRO)
        if daily_budget is not None:
            change["budget_in_micro_currency"] = int(daily_budget * MICRO)
        self._request("PATCH", f"/ad_accounts/{self.ad_account}/ad_groups", json=[change])

    def update_campaign_cap(self, campaign_id: str, lifetime_cap: float) -> None:
        if not self.ads_live or campaign_id.startswith("dry-"):
            return
        self._request("PATCH", f"/ad_accounts/{self.ad_account}/campaigns", json=[{
            "id": campaign_id, "ad_account_id": self.ad_account, "lifetime_spend_cap": int(lifetime_cap * MICRO)}])

    def set_status(self, kind: str, ids: list[str], status: str) -> None:
        """kind is "campaigns" or "ad_groups"; status is "ACTIVE" or "PAUSED"."""
        if not ids or not self.ads_live:
            return
        self._request("PATCH", f"/ad_accounts/{self.ad_account}/{kind}",
                      json=[{"id": i, "status": status} for i in ids])

    def ad_stats(self, ad_ids: list[str], start: str, end: str) -> dict:
        """Totals per ad: {ad_id: {"spend", "clicks", "impressions"}}. Dry run returns nothing."""
        ids = [i for i in ad_ids if not i.startswith("dry-")]
        if not ids or not self.ads_live:
            return {}
        stats = {}
        for i in range(0, len(ids), 250):
            rows = self._request("GET", f"/ad_accounts/{self.ad_account}/ads/analytics", params={
                "ad_ids": ids[i:i + 250], "start_date": start, "end_date": end,
                "granularity": "TOTAL",
                "columns": "SPEND_IN_MICRO_DOLLAR,TOTAL_CLICKTHROUGH,PAID_IMPRESSION"})
            for r in rows:
                stats[r["AD_ID"]] = {"spend": (r.get("SPEND_IN_MICRO_DOLLAR") or 0) / MICRO,
                                     "clicks": r.get("TOTAL_CLICKTHROUGH") or 0,
                                     "impressions": r.get("PAID_IMPRESSION") or 0}
        return stats

    def campaign_daily_spend(self, campaign_id: str, start: str, end: str) -> dict:
        """{date: dollars spent} for the campaign."""
        if campaign_id.startswith("dry-") or not self.ads_live:
            return {}
        rows = self._request("GET", f"/ad_accounts/{self.ad_account}/campaigns/analytics", params={
            "campaign_ids": [campaign_id], "start_date": start, "end_date": end,
            "granularity": "DAY", "columns": "SPEND_IN_MICRO_DOLLAR"})
        return {r["DATE"]: (r.get("SPEND_IN_MICRO_DOLLAR") or 0) / MICRO for r in rows}


def _volume(text) -> float | None:
    """Search volume as a number. The API types it as a string, which may be a plain number or a
    range such as "10K-100K"; a range counts as its lower end."""
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", str(text or ""), re.I)
    if not m:
        return None
    return float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]
