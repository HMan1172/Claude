"""Load the candidate products you want the agents to consider."""
import csv
from urllib.parse import urlparse

REQUIRED = ["id", "name", "url", "price", "commission_rate"]


def load_products(path: str) -> list[dict]:
    """Read products.csv. commission_rate can be written as 0.04 or 4%."""
    products = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
        for row in reader:
            if not (row.get("id") or "").strip():
                continue
            rate = row["commission_rate"].strip()
            rate = float(rate.rstrip("%")) / 100 if rate.endswith("%") else float(rate)
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
            })
    if not products:
        raise ValueError(f"No products found in {path}")
    return products
