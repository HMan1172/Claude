"""Saves a run: pins.csv to post from, pin images, a tracking sheet to fill in, and a readable report."""
import csv
import html
import json
import os
import re

from .dataflows.reports import write_tracking_template
from .images import draw_pin

PIN_COLUMNS = ["post_date", "post_time", "key", "board", "title", "description", "link",
               "alt_text", "image", "keywords"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


def save_run(state: dict, config: dict) -> str:
    run_dir = os.path.join(config["results_dir"], f"{state['run_date']}_{_slug(state['niche'])}")
    img_dir = os.path.join(run_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    pins = state.get("final", {}).get("pins", [])

    for p in pins:
        p["image"] = os.path.join("images", f"{p['key']}.png")
        draw_pin(p, os.path.join(run_dir, p["image"]))

    with open(os.path.join(run_dir, "pins.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PIN_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for p in pins:
            w.writerow({**p, "keywords": ", ".join(p.get("keywords", []))})

    write_tracking_template(os.path.join(run_dir, "tracking.csv"), pins)

    with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in state.items() if k != "products"}, f, indent=1, default=str)

    _write_report(run_dir, state, pins)
    return run_dir


def _write_report(run_dir: str, state: dict, pins: list[dict]) -> None:
    e = html.escape
    trends = state.get("trends", {})
    rows = "".join(
        f"<div class='pin'><img src='{e(p['image'])}' alt='{e(p['alt_text'])}'>"
        f"<div><p class='meta'>{e(p['post_date'])} {e(p['post_time'])} · {e(p['board'])}</p>"
        f"<h3>{e(p['title'])}</h3><p>{e(p['description'])}</p>"
        f"<p class='meta'><a href='{e(p['link'])}'>{e(p['link'])}</a></p></div></div>"
        for p in pins
    )
    shortlist = "".join(
        f"<li><b>{e(s['name'])}</b> (${s['price']:.2f}, {s['commission_rate']:.0%} → "
        f"${s['expected_commission']:.2f} per sale): {e(str(s.get('reason', '')))}</li>"
        for s in state.get("shortlist", [])
    )
    dropped = state.get("final", {}).get("dropped", [])
    review = state.get("review", {})
    dropped_html = "".join(f"<li>{e(k)}: {e('; '.join(review.get(k, {}).get('issues', [])))}</li>"
                           for k in dropped) or "<li>None</li>"
    log = "".join(f"<li>{e(x)}</li>" for x in state.get("log", []))
    page = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>PinAgents run</title><style>
:root{{--bg:#fafaf7;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e0d8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ecebe6;--muted:#9a9890;--line:#2c2b28}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px}}
h1{{font-size:1.5rem}} .meta{{color:var(--muted);font-size:.85rem}} a{{color:inherit;word-break:break-all}}
.pin{{display:flex;gap:16px;border-top:1px solid var(--line);padding:16px 0}}
.pin img{{width:160px;height:240px;object-fit:cover;border-radius:8px;flex:none}}
@media (max-width:560px){{.pin{{flex-direction:column}}.pin img{{width:100%;height:auto}}}}
</style></head><body>
<h1>{e(state['niche'])}: {len(pins)} pins ready</h1>
<p class='meta'>Run date {e(state['run_date'])}. Season: {e(str(trends.get('season','')))}.
Themes: {e(', '.join(trends.get('themes', [])))}</p>
<h2>Shortlisted products</h2><ul>{shortlist}</ul>
<h2>Posting schedule</h2>{rows}
<h2>Dropped by compliance</h2><ul>{dropped_html}</ul>
<h2>Agent log</h2><ul>{log}</ul>
</body></html>"""
    with open(os.path.join(run_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(page)
