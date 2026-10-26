"""Load the candidate products you want the agents to consider."""
import csv
from urllib.parse import urlparse

REQUIRED = ["id", "name", "url", "price", "commission_rate"]


def _rate(value: str) -> float:
    value = value.strip()
    return float(value.rstrip("%")) / 100 if value.endswith("%") else float(value)


def load_products(path: str) -> list[dict]:
    """Read products.csv. commission_rate (and optional conversion_rate) can be 0.04 or 4%.
    Optional epc is the program's earnings per click in dollars, as the network reports it."""
    products = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
        for row in reader:
            if not (row.get("id") or "").strip():
                continue
            rate = _rate(row["commission_rate"])
            price = float(row["price"])
            products.append({
                "id": row["id"].strip(),
                "name": row["name"].strip(),
                "url": row["url"].strip(),
                "price": price,
                "commission_rate": rate,
                "expected_commission": round(price * rate, 2),
                "program": (row.get("program") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
                "domain": urlparse(row["url"].strip()).netloc.lower(),
                # Share of clicks that buy, as your affiliate network reports it for this program.
                "conversion_rate": _rate(row["conversion_rate"]) if (row.get("conversion_rate") or "").strip() else None,
                "epc": float(row["epc"]) if (row.get("epc") or "").strip() else None,
            })
    if not products:
        raise ValueError(f"No products found in {path}")
    return products
