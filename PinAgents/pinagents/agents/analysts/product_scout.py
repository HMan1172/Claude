"""Product Scout: ranks candidate products by how well they fit the niche and how much each sale pays.

Plays the part of TradingAgents' fundamentals analyst.
"""
import json

SYSTEM = """You are the Product Scout on a team that runs one Pinterest account in a single niche.
Rank candidate affiliate products. Favor products that:
- clearly fit the niche and the people who browse it on Pinterest,
- pay a meaningful commission per sale (expected_commission is price x rate),
- look good in a visual pin and solve a specific problem.
Penalize products that need health, money, or safety claims to sell.
Use the lessons from past runs if any are given.
Reply with JSON only: {"ranked": [{"id": str, "score": 0-10, "reason": str, "angle": str}]}
"angle" is the one-line hook a pin about this product should use."""


def create_product_scout(llm, config):
    def node(state):
        products = state["products"]
        user = (
            f"Niche: {state['niche']}\n"
            f"Lessons from past runs:\n{state.get('lessons') or 'None yet.'}\n\n"
            f"Candidates:\n{json.dumps(products, indent=1)}"
        )
        out = llm.json(SYSTEM, user, tier="quick", task="scout",
                       payload={"products": products, "niche": state["niche"]})
        by_id = {p["id"]: p for p in products}
        shortlist = []
        for r in sorted(out.get("ranked", []), key=lambda r: -float(r.get("score", 0))):
            prod = by_id.get(str(r.get("id")))
            if prod and prod not in shortlist:
                shortlist.append({**prod, "score": r.get("score"), "reason": r.get("reason", ""),
                                  "angle": r.get("angle", "")})
            if len(shortlist) >= config["max_products"]:
                break
        return {"shortlist": shortlist,
                "log": [f"Product Scout shortlisted {len(shortlist)} of {len(products)} products."]}
    return node
