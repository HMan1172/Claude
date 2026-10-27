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
   If you'll run ads, also add `conversion_rate`: the share of clicks that buy, as your affiliate
   network reports it for that program (CJ, Impact and ShareASale show it or EPC per program).
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
pinagents check                                      # test every connection, read-only
pinagents autopilot --loop 24                        # niche and products picked from CJ
pinagents autopilot --niche "home office" --loop 24  # or choose the niche
pinagents autopilot --niche "home office" --products products.csv  # or use your own list
```

Run `pinagents check` first: it calls Pinterest and CJ read-only, sends your AI provider one tiny
test message per model, and prints OK or the exact error for each, so a mismatch with either API shows up before anything is posted or spent. It
also shows Pinterest's own click price forecast and expected weekly clicks for your budget (if
Pinterest has opened its beta estimate API to your app).

**Does a click earn more than it costs?** The autopilot checks this on every click, not per
product: it bids 40% of what a click is expected to earn, and Pinterest never charges more than
the bid, so every click it buys costs less than half its expected value. Clicks priced above that
are simply lost auctions. Expected value starts from the network's EPC and is corrected by what
your own ads actually earn. Blocking whole products when Pinterest's typical price looked too
high was tested in the simulator and earned less, because those products still win cheaper clicks.

Or run one cycle a day from cron: `15 7 * * * cd /path/to/PinAgents && .venv/bin/pinagents autopilot --niche "home office" --products products.csv >> autopilot.log 2>&1`

Each cycle:

1. Refreshes the Pinterest access token (needs `PINTEREST_APP_ID`, `PINTEREST_APP_SECRET`,
   `PINTEREST_REFRESH_TOKEN` once; the rotating refresh token is then kept in `~/.pinagents/`).
2. When no pins are waiting, the agents write a new batch (using lessons from earlier results).
   Without `--products`, it first searches every CJ Affiliate program you've joined for products
   matching the niche, best earnings per click first, and passes the top 40 to the Product Scout.
   Without `--niche`, it picks the category of your best-earning joined programs and keeps it.
3. Posts the pins that are due, at most `pins_per_day`.
4. Runs ads, all in plain code:
   - One traffic campaign with Pinterest's own lifetime spend cap set to `ads_total_cap`.
   - One ad group per product, paying per outbound click with the **bid capped at 40% of what a
     click should earn**: `conversion_rate x price x commission x 0.4` (`assumed_conversion_rate`,
     1.75%, when the CSV has none). Products that can't afford Pinterest's $0.10 minimum bid are
     never advertised, and neither are Amazon links (see below).
   - Every dollar of daily budget is authorized by `BudgetGuard` first; real spend from Pinterest
     replaces the authorized amount once a day is over.
   - Bids are scaled down by how our own ads earn against the network's EPC, pooled across all
     products and weighted like 5 average sales, so one unlucky week can't crash them.
   - A product is paused when it has 20+ clicks and has earned less than it cost, or when it has
     spent `ads_test_spend_per_product` without proving it earns more than it costs.
   - `~/.pinagents/STOP` pauses the campaign on the next cycle.
5. Once a week, ad results become lessons for the agents' next batch.

Until you set `PINAGENTS_ALLOW_POSTING=1` / `PINAGENTS_ALLOW_ADS=1` (plus a token, board and ad
account), it is a dry run with its own state and ledger files, so you can watch a few cycles first.

**Products and earnings from CJ.** With `CJ_API_TOKEN`, `CJ_COMPANY_ID` and `CJ_WEBSITE_ID` set,
autopilot finds products itself and pulls earnings every cycle from CJ's commission report. Each
product's link carries its own `sid`, so sales are matched to products automatically, and the
program's EPC sets the bid. Ads use the same link with `sid=<id>-ad`, so ad sales are counted
apart from free-pin sales. It can only use programs you've joined: each advertiser approves its
own publishers, often by hand.

Other networks: write `PINAGENTS_EARNINGS_CSV` (`product_id,date,earnings`) from your network's
report. Without earnings data, no product can prove it's profitable, so each one stops after its
$20 test budget.

**Know before you switch ads on.**
- Amazon Associates' agreement (effective April 14, 2026) disqualifies purchases referred through
  any paid or boosted ad linking to Amazon, so autopilot never advertises amazon.* links. Organic
  pins are unaffected. Ads need brand programs that allow paid social traffic.
- Published 2026 benchmarks put most Pinterest clicks at $0.30-1.50 (home decor $0.10-0.50),
  and Pinterest's US minimum CPC bid is $0.10. A click on a $35 product at 4% commission earns
  1-3 cents, so it can never pay for itself; ads only work for high-commission programs.
- Check each program's terms on paid ads, and Pinterest reviews every ad against its ad policies.

## Simulating ad results

`python experiments/simulate.py` runs the autopilot's real ad code against a simulated Pinterest
auction for 60 days per trial ($5/day, $100 total cap, 5 products). Click prices, commission
rates and conversion rates come from published benchmarks where they exist (see the comments in
the script); the rest are assumptions. Read it as how the rules behave, not as a forecast.

Mean profit per 60 days at typical click prices ($0.25-1.00 median), 200 trials each, with and
without a network-reported `conversion_rate` (simulated as up to ~40% wrong):

| Scenario | Without conversion_rate | With conversion_rate | Worst 10% (with) |
|---|---|---|---|
| Home brand programs (4-12% commission) | +$12, 14% of runs profit | +$51, 59% | about -$1 |
| Top brand programs (10-20%) | +$134, 80% | +$152, 87% | about $0 |
| High-ticket (15-40%, rare sales) | +$86, 76% | +$229, 90% | about -$6 |
| Weak conversion | $0 (never bids) | $0 | $0 |

In a cheap market ($0.10-0.50) profits roughly double; in an expensive one ($0.70-2.90) they
mostly vanish (+$2 to +$70) and high-ticket's worst 10% lose about $30.

Tried and rejected: bids that learn from early sales (sales are too rare, so good products got cut),
moving freed budget to surviving products, raising bids on winners (on a fixed budget that
just buys pricier clicks), and growing budgets only on proven winners (no better than a flat budget).

### Scaling up

In the simulator, profit grows with the number of products and the budget per product, not with
cleverer rules. Weekly profit after 8 weeks, 30 products at $20/day each (`PINAGENTS_MAX_PRODUCTS=30`,
`PINAGENTS_AD_GROUP_BUDGET=20`, `PINAGENTS_ADS_DAILY_CAP=600`, `PINAGENTS_PINS_PER_DAY=10`):

| Programs | Cheap clicks ($0.10-0.50) | Typical clicks ($0.25-1.00) |
|---|---|---|
| Home brands (4-12%) | ~$430/week on ~$225 spend | ~$80/week on ~$50 spend |
| Top brands (10-20%) | ~$2,400/week on ~$830 spend | ~$600/week on ~$300 spend |
| High-ticket (15-40%) | ~$5,800/week on ~$1,700 spend | ~$1,900-2,100/week on ~$730-870 spend |

The upper rows rest on an unsourced assumption: that each product can win 50-400 auctions a day.
If Pinterest has fewer clicks to sell in your niche, scale stops there whatever the budget. Raise
budgets in steps and only after `earnings.csv` shows real ad-driven sales.

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
