"""Trend Analyst: what people in this niche are likely searching for on Pinterest right now.

Plays the part of TradingAgents' news and sentiment analysts. Runs in parallel with the scout.
When Pinterest's trends API is available, it works from real trending searches instead of guessing.
"""
import json
from datetime import date

SYSTEM = """You are the Trend Analyst on a team that runs one Pinterest account.
Pinterest users plan ahead: they search for seasons, holidays and events weeks before they happen.
Given the niche and today's date, list what this audience is likely planning and searching for over
the next 4-8 weeks. Be specific to the niche. Do not invent statistics.
Reply with JSON only:
{"season": str, "themes": [str, ...up to 5], "keywords": [str, ...up to 10], "avoid": [str]}"""


def create_trend_analyst(llm, config):
    def node(state):
        today = state.get("run_date") or date.today().isoformat()
        month = date.fromisoformat(today).strftime("%B")
        user = f"Niche: {state['niche']}\nToday: {today}"
        if state.get("pinterest_trends"):
            user += ("\nReal Pinterest search trends for this niche (growth is % change in searches; "
                     "base your themes and keywords on these and prefer fast-growing, seasonal ones):\n"
                     + json.dumps(state["pinterest_trends"]))
        out = llm.json(SYSTEM, user, tier="quick", task="trends",
                       payload={"niche": state["niche"], "month": month})
        trends = {
            "season": out.get("season", month),
            "themes": list(out.get("themes", []))[:5],
            "keywords": list(out.get("keywords", []))[:10],
            "avoid": list(out.get("avoid", [])),
        }
        return {"trends": trends, "log": [f"Trend Analyst found {len(trends['themes'])} themes."]}
    return node
