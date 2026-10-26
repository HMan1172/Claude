"""Stage 2: posting through Pinterest's official v5 API.

Posting needs a Business account, a developer app, and Standard access (Trial access is
mostly read-only). Until config["allow_posting"] is True, every call is a dry run that
only prints what it would send.
"""
import base64
import os

import requests

API = "https://api.pinterest.com/v5"


class PinterestClient:
    def __init__(self, config: dict):
        self.config = config
        self.token = os.getenv("PINTEREST_ACCESS_TOKEN", "")

    @property
    def live(self) -> bool:
        return bool(self.config.get("allow_posting") and self.token)

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
            return {"dry_run": True, "would_post": {k: v for k, v in body.items() if k != "media_source"}}
        if not board_id:
            raise ValueError("Set PINTEREST_BOARD_ID before posting.")
        resp = requests.post(f"{API}/pins", json=body, timeout=30,
                             headers={"Authorization": f"Bearer {self.token}"})
        resp.raise_for_status()
        return resp.json()
