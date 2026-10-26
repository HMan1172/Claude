"""Compliance Checker: blocks pins that could get the account flagged or mislead people.

Plays the part of TradingAgents' risk team. Hard rules run in plain code first; the model
then reviews wording for unprovable claims or fake testimonials.
"""
import json
import re
from urllib.parse import urlparse

SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly",
              "rebrand.ly", "cutt.ly", "shorturl.at", "tiny.cc", "rb.gy", "linktr.ee"}
RISKY_PHRASES = [
    r"\bcures?\b", r"\bguarantee[sd]?\b", r"\bmiracle\b", r"\blose \d+ ?(lbs?|pounds|kg)\b",
    r"\bget rich\b", r"\brisk[- ]free\b", r"\bdoctors? (hate|recommend)\b", r"\b100% (safe|effective)\b",
    r"\bI (bought|tried|used|own)\b", r"\bmy (husband|wife|kids?) (love|loves)\b",
]

SYSTEM = """You are the Compliance Checker for a Pinterest affiliate account. Review each pin's
title, description and overlay text. Reject a pin if it:
- makes health, weight-loss, financial or safety claims that can't be proven,
- pretends the account owner personally used the product, or invents reviews/ratings/prices,
- is misleading, clickbait, or doesn't match the product it links to.
Everything else passes. Be strict but don't nitpick style.
Reply with JSON only: {"results": [{"key": str, "ok": bool, "issues": [str]}]}"""


def hard_checks(pin: dict, disclosure: str) -> list[str]:
    issues = []
    if not pin["description"].startswith(disclosure):
        issues.append("Affiliate disclosure must open the description.")
    if not pin["title"]:
        issues.append("Missing title.")
    if len(pin["title"]) > 100:
        issues.append(f"Title is {len(pin['title'])} characters; Pinterest allows 100.")
    if len(pin["description"]) > 500:
        issues.append(f"Description is {len(pin['description'])} characters; Pinterest allows 500.")
    if len(pin.get("overlay_text", "")) > 40:
        issues.append("Overlay text over 40 characters won't fit the image.")
    url = urlparse(pin["link"])
    if url.scheme != "https":
        issues.append("Link must use https.")
    if url.netloc.lower().removeprefix("www.") in SHORTENERS:
        issues.append("Link shorteners and cloaked links aren't allowed; use the full affiliate URL.")
    text = " ".join([pin["title"], pin["description"], pin.get("overlay_text", "")])
    for pattern in RISKY_PHRASES:
        if re.search(pattern, text, re.I):
            issues.append(f"Risky wording matched '{pattern}'.")
    return issues


def create_compliance_checker(llm, config):
    def node(state):
        drafts = state["drafts"]
        disclosure = config["disclosure"]
        results = {d["key"]: {"ok": True, "issues": hard_checks(d, disclosure)} for d in drafts}

        to_review = [{"key": d["key"], "product": d["product_name"], "title": d["title"],
                      "description": d["description"], "overlay_text": d["overlay_text"]}
                     for d in drafts]
        if to_review:
            out = llm.json(SYSTEM, json.dumps(to_review, indent=1), tier="deep",
                           task="compliance_review", payload={"keys": [d["key"] for d in drafts]})
            for r in out.get("results", []):
                key = r.get("key")
                if key in results and not r.get("ok", True):
                    results[key]["issues"].extend(r.get("issues") or ["Rejected by reviewer."])

        for r in results.values():
            r["ok"] = not r["issues"]
        rejected = sum(1 for r in results.values() if not r["ok"])
        return {"review": results,
                "log": [f"Compliance passed {len(results) - rejected}, rejected {rejected}."]}
    return node
