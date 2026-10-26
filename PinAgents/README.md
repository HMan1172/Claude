# PinAgents

A multi-agent LLM framework for running one Pinterest affiliate account, built the same way as
[TradingAgents](https://github.com/TauricResearch/TradingAgents): specialized agents wired together
with LangGraph, a config dict, a CLI, and a memory log that carries lessons into the next run.

```
START ─┬─> Product Scout ──┐
       └─> Trend Analyst ──┴─> Pin Writer ─> Compliance ─┬─> Content Manager ─> END
                                   ^                       │
                                   └──── revise rejected ──┘
```

| Agent | TradingAgents equivalent | Job |
|---|---|---|
| Product Scout | Fundamentals analyst | Ranks your candidate products by niche fit and commission per sale |
| Trend Analyst | News / sentiment analysts | What this audience is planning and searching for in the next 4-8 weeks |
| Pin Writer | Trader | Writes pin titles, descriptions, image text and alt text |
| Compliance Checker | Risk team | Blocks missing disclosures, shortened links, unprovable claims, fake testimonials |
| Content Manager | Portfolio manager | Approves the final set and builds the posting schedule |
| Performance Analyst | Memory log / backtest | Scores results per click and writes lessons for the next run |

## Stages

1. **Stage 1 (this release): no money.** Agents pick products and write pins with images.
   You post them (by hand or with Pinterest's scheduler) and fill in the tracking sheet.
2. **Stage 2: automatic posting** through Pinterest's official v5 API. Needs a Business account,
   a developer app and Standard access. The client is in `pinagents/dataflows/pinterest_api.py`
   and runs as a dry run until you set `allow_posting: True`.
3. **Stage 3: ads.** `pinagents autopilot` runs Stages 1-3 unattended (see below).
   `BudgetGuard` enforces a daily cap, a total cap and a kill switch in plain code.

## Install

Python 3.10 or later.

```bash
cd PinAgents
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[anthropic]"      # or ".[openai]"; plain "." is enough for mock and Ollama
cp .env.example .env               # then set your provider and key
```

## Use

1. Copy `products_example.csv` to `products.csv` and fill in real products from your affiliate
   programs: `id, name, url, price, commission_rate` (as `0.04` or `4%`), plus optional `program, notes`.
   Put 10-30 candidates in; the scout keeps the best 5.

2. Run the agents:

   ```bash
   pinagents run --niche "home office" --products products.csv
   ```

   Output lands in `results/<date>_<niche>/`: `report.html` to read, `pins.csv` with every
   title, description, link and posting time, `images/` with one 1000x1500 image per pin, and
   `tracking.csv` to fill in.

3. Post the pins. After a couple of weeks, copy impressions, outbound clicks and saves from
   Pinterest Analytics, and earnings per product from your affiliate dashboard, into `tracking.csv`.

4. Score it and save lessons for next time:

   ```bash
   pinagents track --niche "home office" --sheet results/2026-10-25_home-office/tracking.csv
   ```

   This prints earnings per click and the most you could pay per ad click. The lessons are saved
   to `~/.pinagents/lessons.md`, and the scout and writer read them on the next run.

5. Check whether ads could ever pay:

   ```bash
   pinagents economics --conversion 0.05 --order 40 --commission 0.04 --cpc 0.15
   ```

Try everything for free first with `--provider mock`. It runs offline with placeholder text so you
can see the whole flow before paying for an API key.

## Autopilot: no human in the loop

```bash
pinagents autopilot --niche "home office" --products products.csv            # one cycle
pinagents autopilot --niche "home office" --products products.csv --loop 24  # keep going
```

Or run one cycle a day from cron: `15 7 * * * cd /path/to/PinAgents && .venv/bin/pinagents autopilot --niche "home office" --products products.csv >> autopilot.log 2>&1`

Each cycle:

1. Refreshes the Pinterest access token (needs `PINTEREST_APP_ID`, `PINTEREST_APP_SECRET`,
   `PINTEREST_REFRESH_TOKEN` once; the rotating refresh token is then kept in `~/.pinagents/`).
2. When no pins are waiting, the agents write a new batch (using lessons from earlier results).
3. Posts the pins that are due, at most `pins_per_day`.
4. Runs ads, all in plain code:
   - One traffic campaign with Pinterest's own lifetime spend cap set to `ads_total_cap`.
   - One ad group per product, paying per outbound click with the **bid capped at the most a click
     can earn**: `assumed_conversion_rate x price x commission x 0.7`. Products that can't afford
     Pinterest's minimum bid are never advertised.
   - Every dollar of daily budget is authorized by `BudgetGuard` first; real spend from Pinterest
     replaces the authorized amount once a day is over.
   - A product is paused when it has 20+ clicks and has earned less than it cost, or when it has
     spent `ads_test_spend_per_product` without proving it earns more than it costs.
   - `~/.pinagents/STOP` pauses the campaign on the next cycle.
5. Once a week, ad results become lessons for the agents' next batch.

Until you set `PINAGENTS_ALLOW_POSTING=1` / `PINAGENTS_ALLOW_ADS=1` (plus a token, board and ad
account), it is a dry run with its own state and ledger files, so you can watch a few cycles first.

**Earnings data.** Affiliate networks don't share one API, so autopilot reads
`PINAGENTS_EARNINGS_CSV` (`product_id,date,earnings`). Have your network's export or report
script write it. Use a separate tracking ID for ad links so it only counts ad-driven sales.
Without it, no product can prove it's profitable, so each one stops after its test budget.

**Know before you switch ads on.** At typical numbers (2% of clicks buy, 4% commission), a click
on a $35 product earns about 3 cents, below the 10-cent `ads_min_bid` default, so autopilot will
refuse to advertise it. Set `ads_min_bid` to the real minimum Pinterest Ads Manager shows you. Ads only make sense for high-commission products. Many affiliate programs (Amazon
Associates included) restrict paid ads with their links, and Pinterest reviews every ad against
its own ad policies, so check both before turning ads on.

## Python

```python
from pinagents.graph.pin_graph import PinAgentsGraph
from pinagents.default_config import DEFAULT_CONFIG
from pinagents.output import save_run

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "anthropic"
config["pins_per_product"] = 3

pa = PinAgentsGraph(config, debug=True)
state = pa.propagate("home office", "products.csv")
print(save_run(state, config))
```

## Providers

| Provider | Setting | Needs |
|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` from console.anthropic.com (billed separately from a Claude Pro plan) |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Ollama | `ollama` | Ollama running locally; free |
| Mock | `mock` | Nothing; placeholder output for testing |

## Rules the code enforces

- The affiliate disclosure is added to the start of every description in code, so it can't be left out.
- Shortened or cloaked links and non-https links are rejected.
- Unprovable health, weight, money or safety claims and first-person "I bought this" testimonials are rejected.
- Pins that still fail after two rewrites are dropped, never scheduled.
- Ad spend can only go through `BudgetGuard`. Create `~/.pinagents/STOP` to halt all spending.

Also check each affiliate program's own terms. Many limit where and how their links can be promoted,
especially in paid ads.

## Tests

```bash
pip install -e ".[dev]"
pytest
```
