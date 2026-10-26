"""Deterministic stand-in for a real model. Lets you test the whole pipeline offline for free.

The output is plausible but generic. Use a real provider for real pins.
"""


def respond(task: str, payload):
    payload = payload or {}
    handler = _HANDLERS.get(task)
    if handler is None:
        return {}
    return handler(payload)


def _scout(p):
    ranked = []
    for prod in p.get("products", []):
        ranked.append({
            "id": prod["id"],
            "score": round(min(10, 4 + prod.get("expected_commission", 0)), 1),
            "reason": "Higher commission per sale and fits the niche.",
            "angle": f"Why {prod['name']} is worth it",
        })
    ranked.sort(key=lambda r: -r["score"])
    return {"ranked": ranked}


def _trends(p):
    niche = p.get("niche", "this niche")
    return {
        "season": p.get("month", ""),
        "themes": [f"{niche} on a budget", f"best {niche} picks this month", f"beginner {niche} guide"],
        "keywords": [niche, f"{niche} ideas", f"{niche} essentials"],
        "avoid": ["clickbait claims"],
    }


def _write(p):
    pins = []
    for prod in p.get("products", []):
        for v in range(1, p.get("variants", 2) + 1):
            pins.append({
                "product_id": prod["id"],
                "variant": v,
                "title": f"{prod['name']}: honest pick #{v}"[:100],
                "description": f"A quick look at {prod['name']} and who it suits. Price around ${prod.get('price', '')}.",
                "overlay_text": f"{prod['name']}"[:40],
                "board": p.get("niche", "Picks").title(),
                "keywords": [p.get("niche", "")],
                "alt_text": f"Graphic about {prod['name']}",
            })
    return {"pins": pins}


def _review(p):
    return {"results": [{"key": k, "ok": True, "issues": []} for k in p.get("keys", [])]}


def _manager(p):
    return {"order": p.get("keys", []), "notes": "Spread products across days so no product repeats back to back."}


def _lessons(p):
    best = p.get("best", [])
    worst = p.get("worst", [])
    lines = []
    if best:
        lines.append(f"- Best earner per click: {best[0]['key']}. Make more pins like it.")
    if worst:
        lines.append(f"- Weakest per click: {worst[0]['key']}. Try a different angle or drop the product.")
    return {"lessons": lines or ["- Not enough clicks yet to learn anything. Keep posting."]}


_HANDLERS = {
    "scout": _scout,
    "trends": _trends,
    "write": _write,
    "compliance_review": _review,
    "manager": _manager,
    "lessons": _lessons,
}
