"""Read the tracking sheet you fill in from Pinterest analytics and your affiliate dashboard."""
import csv

TRACKING_COLUMNS = ["pin_key", "product_id", "title", "posted_date",
                    "impressions", "outbound_clicks", "saves", "earnings"]


def write_tracking_template(path: str, pins: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACKING_COLUMNS)
        w.writeheader()
        for p in pins:
            w.writerow({"pin_key": p["key"], "product_id": p["product_id"], "title": p["title"],
                        "posted_date": "", "impressions": "", "outbound_clicks": "",
                        "saves": "", "earnings": ""})


def _num(value) -> float:
    value = (value or "").strip().replace("$", "").replace(",", "")
    return float(value) if value else 0.0


def load_tracking(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (row.get("pin_key") or "").strip():
                continue
            rows.append({
                "key": row["pin_key"].strip(),
                "product_id": (row.get("product_id") or "").strip(),
                "title": (row.get("title") or "").strip(),
                "posted": bool((row.get("posted_date") or "").strip()),
                "impressions": _num(row.get("impressions")),
                "clicks": _num(row.get("outbound_clicks")),
                "saves": _num(row.get("saves")),
                "earnings": _num(row.get("earnings")),
            })
    return rows
