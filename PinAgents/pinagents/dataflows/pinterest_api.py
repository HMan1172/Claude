"""Pinterest's official v5 API: posting pins (Stage 2) and running ads (Stage 3).

Posting needs a Business account, a developer app, and Standard access (Trial access is
mostly read-only). Ads also need an ad account with billing set up and the ads:write scope.
Until config["allow_posting"] / config["allow_ads"] are True, every write is a dry run that
returns a fake id, so the autopilot can be tested end to end without touching your account.
"""
import base64
import json
import os
import time

import requests

API = "https://api.pinterest.com/v5"
MICRO = 1_000_000


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
        resp = requests.post(f"{API}/oauth/token", timeout=30, auth=(app_id, secret),
                             data={"grant_type": "refresh_token", "refresh_token": refresh})
        resp.raise_for_status()
        body = resp.json()
        self.token = body["access_token"]
        if body.get("refresh_token"):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"refresh_token": body["refresh_token"], "saved_at": int(time.time())}, f)
        return True

    def _request(self, method: str, path: str, **kw):
        resp = requests.request(method, f"{API}{path}", timeout=30,
                                headers={"Authorization": f"Bearer {self.token}"}, **kw)
        resp.raise_for_status()
        return resp.json()

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
        return self._request("POST", "/pins", json=body)

    # ---- ads ------------------------------------------------------------
    def _batch_create(self, kind: str, item: dict) -> str:
        if not self.ads_live:
            return self._dry_id(kind.rstrip("s"))
        out = self._request("POST", f"/ad_accounts/{self.ad_account}/{kind}", json=[item])
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

    def create_ad(self, ad_group_id: str, pin_id: str, name: str) -> str:
        return self._batch_create("ads", {"ad_group_id": ad_group_id, "pin_id": pin_id,
                                          "creative_type": "REGULAR", "status": "ACTIVE", "name": name})

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
                "ad_ids": ",".join(ids[i:i + 250]), "start_date": start, "end_date": end,
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
            "campaign_ids": campaign_id, "start_date": start, "end_date": end,
            "granularity": "DAY", "columns": "SPEND_IN_MICRO_DOLLAR"})
        return {r["DATE"]: (r.get("SPEND_IN_MICRO_DOLLAR") or 0) / MICRO for r in rows}
