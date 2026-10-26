"""Pin Writer: turns shortlisted products and trends into pin drafts.

Plays the part of TradingAgents' trader: it acts on the analysts' reports. On a revision
round it rewrites only the pins compliance rejected, using the reviewer's notes.
The affiliate disclosure is added in code, so it can never be left out.
"""
import json

SYSTEM = """You are the Pin Writer for one Pinterest account. Write pins people will save and click.
Rules:
- title: at most 90 characters, specific and useful, no ALL CAPS, no fake urgency.
- description: at most 380 characters (a disclosure is added in front of it later). Say who the
  product is for and one concrete reason it helps. Work in 2-3 of the given keywords naturally.
- overlay_text: the big text printed on the pin image, at most 40 characters.
- Never claim results you can't prove (health, weight, money, safety). Never pretend to be a
  customer who used the product, and never invent reviews, ratings or prices.
- Each variant of the same product must use a different angle.
Reply with JSON only:
{"pins": [{"product_id": str, "variant": int, "title": str, "description": str,
           "overlay_text": str, "board": str, "keywords": [str], "alt_text": str}]}"""


def pin_key(pin: dict) -> str:
    return f"{pin['product_id']}-v{pin['variant']}"


def _finalize(pin: dict, product: dict, disclosure: str) -> dict:
    desc = pin.get("description", "").strip()
    if desc.startswith(disclosure):
        desc = desc[len(disclosure):].strip()
    full_desc = f"{disclosure} {desc}"
    out = {
        "product_id": product["id"],
        "variant": int(pin.get("variant", 1)),
        "title": pin.get("title", "").strip(),
        "description": full_desc,
        "overlay_text": pin.get("overlay_text", product["name"]).strip(),
        "board": pin.get("board", "").strip(),
        "keywords": list(pin.get("keywords", [])),
        "alt_text": pin.get("alt_text", "").strip(),
        "link": product["url"],
        "product_name": product["name"],
    }
    out["key"] = pin_key(out)
    return out


def create_pin_writer(llm, config):
    def node(state):
        disclosure = config["disclosure"]
        shortlist = state["shortlist"]
        by_id = {p["id"]: p for p in shortlist}
        review = state.get("review") or {}
        drafts = state.get("drafts") or []
        rnd = state.get("revision_round", 0)

        if review:  # revision round: rewrite rejected pins only
            rejected = [d for d in drafts if not review.get(d["key"], {}).get("ok", True)]
            keep = [d for d in drafts if review.get(d["key"], {}).get("ok", True)]
            notes = {d["key"]: review[d["key"]]["issues"] for d in rejected}
            user = (
                "Rewrite these rejected pins so they pass review. Keep product_id and variant.\n"
                f"Reviewer notes: {json.dumps(notes)}\n"
                f"Rejected pins: {json.dumps(rejected, indent=1)}"
            )
            targets = [by_id[d["product_id"]] for d in rejected if d["product_id"] in by_id]
            out = llm.json(SYSTEM, user, tier="quick", task="write",
                           payload={"products": [], "niche": state["niche"]})
            rewritten = [_finalize(p, by_id[str(p["product_id"])], disclosure)
                         for p in out.get("pins", []) if str(p.get("product_id")) in by_id]
            # In mock mode nothing comes back; carry rejected pins forward unchanged.
            if not rewritten and targets:
                rewritten = rejected
            return {"drafts": keep + rewritten, "revision_round": rnd + 1,
                    "log": [f"Pin Writer revised {len(rejected)} pins (round {rnd + 1})."]}

        user = (
            f"Niche: {state['niche']}\n"
            f"Trends: {json.dumps(state.get('trends', {}))}\n"
            f"Lessons from past runs:\n{state.get('lessons') or 'None yet.'}\n"
            f"Write {config['pins_per_product']} variants for each product:\n"
            f"{json.dumps(shortlist, indent=1)}"
        )
        out = llm.json(SYSTEM, user, tier="quick", task="write",
                       payload={"products": shortlist, "niche": state["niche"],
                                "variants": config["pins_per_product"]})
        new = []
        for p in out.get("pins", []):
            pid = str(p.get("product_id"))
            if pid in by_id:
                new.append(_finalize(p, by_id[pid], disclosure))
        # de-duplicate keys
        seen, unique = set(), []
        for d in new:
            if d["key"] not in seen:
                seen.add(d["key"])
                unique.append(d)
        return {"drafts": unique, "revision_round": 0, "review": {},
                "log": [f"Pin Writer drafted {len(unique)} pins."]}
    return node
