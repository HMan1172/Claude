"""Performance Analyst: turns your tracking sheet into earnings per click, a max ad price, and lessons."""
import json

from .dataflows.reports import load_tracking
from .economics import max_cpc, measured_epc
from .memory import LessonsLog

SYSTEM = """You are the Performance Analyst for a Pinterest affiliate account. You get each pin's
impressions, outbound clicks, saves and affiliate earnings. Write 3-6 short, specific lessons for the
next round of pins: which products and angles earn per click, which to drop, what to try next.
Only draw conclusions the numbers support; say so when there are too few clicks to tell.
Reply with JSON only: {"lessons": ["- lesson", ...]}"""

MIN_CLICKS = 20  # below this, a pin's earnings per click is mostly noise


def analyze(tracking_csv: str, niche: str, llm, config: dict, write_lessons: bool = True) -> dict:
    rows = [r for r in load_tracking(tracking_csv) if r["posted"] or r["impressions"] or r["clicks"]]
    clicks = sum(r["clicks"] for r in rows)
    earnings = sum(r["earnings"] for r in rows)
    impressions = sum(r["impressions"] for r in rows)
    for r in rows:
        r["epc"] = measured_epc(r["earnings"], r["clicks"])
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0.0

    epc = measured_epc(earnings, clicks)
    ranked = sorted([r for r in rows if r["clicks"] >= MIN_CLICKS], key=lambda r: -r["epc"])
    summary = {
        "pins": len(rows), "impressions": impressions, "clicks": clicks, "earnings": earnings,
        "epc": epc, "max_cpc": max_cpc(epc), "enough_data": clicks >= MIN_CLICKS * 3,
        "best": ranked[:3], "worst": ranked[-3:][::-1] if len(ranked) > 3 else [],
        "rows": rows,
    }

    if write_lessons and rows:
        payload = {"best": summary["best"], "worst": summary["worst"]}
        out = llm.json(SYSTEM, json.dumps({k: v for k, v in summary.items() if k != "rows"}
                                          | {"rows": rows}, default=str),
                       tier="deep", task="lessons", payload=payload)
        lessons = [str(x) for x in out.get("lessons", [])]
        lessons.append(f"- Totals: {int(clicks)} clicks, ${earnings:.2f} earned, "
                       f"${epc:.3f} per click, ads break even below ${epc:.3f} per click.")
        LessonsLog(config["memory_log_path"]).append(niche, lessons)
        summary["lessons"] = lessons
    return summary
