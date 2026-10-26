"""Content Manager: approves the final set and builds the posting schedule.

Plays the part of TradingAgents' portfolio manager. Only pins that passed compliance can be
scheduled; the model only chooses the order, the code enforces the pace.
"""
import json
from datetime import date, timedelta

SYSTEM = """You are the Content Manager for one Pinterest account. You receive pins that already
passed compliance. Decide the posting order: lead with the pins most likely to earn (strong
product fit, higher commission, timely theme), and avoid posting the same product twice in a row.
Reply with JSON only: {"order": [pin keys...], "notes": str}"""


def create_content_manager(llm, config):
    def node(state):
        review = state.get("review") or {}
        approved = [d for d in state.get("drafts", []) if review.get(d["key"], {}).get("ok")]
        dropped = [d["key"] for d in state.get("drafts", []) if not review.get(d["key"], {}).get("ok")]

        summary = [{"key": d["key"], "product": d["product_name"], "title": d["title"]} for d in approved]
        out = llm.json(SYSTEM, json.dumps({"trends": state.get("trends"), "pins": summary}, indent=1),
                       tier="deep", task="manager", payload={"keys": [d["key"] for d in approved]})
        by_key = {d["key"]: d for d in approved}
        order = [k for k in out.get("order", []) if k in by_key]
        order += [k for k in by_key if k not in order]  # never lose an approved pin

        start = date.fromisoformat(state["run_date"]) + timedelta(days=1)
        per_day = max(1, int(config["pins_per_day"]))
        times = config["posting_times"]
        schedule = []
        for i, key in enumerate(order):
            day = start + timedelta(days=i // per_day)
            slot = times[(i % per_day) % len(times)]
            schedule.append({**by_key[key], "post_date": day.isoformat(), "post_time": slot})

        return {"final": {"pins": schedule, "dropped": dropped, "notes": out.get("notes", "")},
                "log": [f"Content Manager scheduled {len(schedule)} pins, dropped {len(dropped)}."]}
    return node
