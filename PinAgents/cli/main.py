"""PinAgents command line.

  pinagents run --niche "home office" --products products.csv
  pinagents track --niche "home office" --sheet results/<run>/tracking.csv
  pinagents economics --conversion 0.05 --order 40 --commission 0.04 --cpc 0.15
  pinagents autopilot --niche "home office" [--products products.csv] [--loop 24]
"""
import argparse
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pinagents.default_config import DEFAULT_CONFIG
from pinagents.economics import ad_profit, expected_epc, max_cpc


def _config(args) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if getattr(args, "provider", None):
        cfg["llm_provider"] = args.provider
    return cfg


def cmd_run(args):
    from pinagents.graph.pin_graph import PinAgentsGraph
    from pinagents.output import save_run

    cfg = _config(args)
    if args.pins_per_product:
        cfg["pins_per_product"] = args.pins_per_product
    if args.max_products:
        cfg["max_products"] = args.max_products
    print(f"Running agents for '{args.niche}' with provider '{cfg['llm_provider']}'...")
    ta = PinAgentsGraph(cfg, debug=True)
    state = ta.propagate(args.niche, args.products, run_date=args.date)
    run_dir = save_run(state, cfg)
    n = len(state.get("final", {}).get("pins", []))
    print(f"\n{n} pins ready in {run_dir}")
    print("  report.html  - read this first")
    print("  pins.csv     - titles, descriptions, links and posting times")
    print("  images/      - one image per pin")
    print("  tracking.csv - fill in after posting, then run `pinagents track`")
    if cfg["llm_provider"] == "mock":
        print("\nNote: provider is 'mock' (offline test). Set PINAGENTS_LLM_PROVIDER for real pins.")


def cmd_track(args):
    from pinagents.llm import LLM
    from pinagents.tracker import analyze

    cfg = _config(args)
    s = analyze(args.sheet, args.niche, LLM(cfg), cfg, write_lessons=not args.no_lessons)
    print(f"Pins with data: {s['pins']}   impressions: {int(s['impressions'])}   "
          f"clicks: {int(s['clicks'])}   earned: ${s['earnings']:.2f}")
    print(f"Earnings per click: ${s['epc']:.3f}")
    print(f"Max you could pay per ad click and keep 30%: ${s['max_cpc']:.3f}")
    if not s["enough_data"]:
        print("Not enough clicks yet to trust these numbers. Keep posting before you buy ads.")
    for r in s["best"]:
        print(f"  strong: {r['key']}  {int(r['clicks'])} clicks  ${r['epc']:.3f}/click")
    for r in s["worst"]:
        print(f"  weak:   {r['key']}  {int(r['clicks'])} clicks  ${r['epc']:.3f}/click")
    for line in s.get("lessons", []):
        print(line)


def cmd_autopilot(args):
    from pinagents.autopilot import run_cycle

    from pinagents.dataflows import cj

    cfg = _config(args)
    if not args.products and not cj.configured():
        raise ValueError("Give --products, or set CJ_API_TOKEN, CJ_COMPANY_ID and CJ_WEBSITE_ID "
                         "so products are found automatically.")
    while True:
        try:
            for line in run_cycle(args.niche, args.products, cfg):
                print(line)
        except Exception as err:
            if not args.loop:
                raise
            print(f"Cycle failed, will retry next cycle. {type(err).__name__}: {err}", file=sys.stderr)
        if not args.loop:
            return
        time.sleep(args.loop * 3600)


def cmd_economics(args):
    epc = expected_epc(args.conversion, args.order, args.commission)
    print(f"Expected earnings per click: ${epc:.3f}")
    print(f"Max cost per click to keep a 30% margin: ${max_cpc(epc):.3f}")
    if args.cpc is not None:
        p = ad_profit(epc, args.cpc, 1000)
        verdict = "profit" if p > 0 else "loss"
        print(f"At ${args.cpc:.2f} per click, 1,000 ad clicks = ${abs(p):,.2f} {verdict}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="pinagents", description="Multi-agent Pinterest affiliate framework")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Have the agents pick products and write a batch of pins")
    r.add_argument("--niche", required=True)
    r.add_argument("--products", required=True, help="CSV of candidate products")
    r.add_argument("--provider", choices=["anthropic", "openai", "ollama", "mock"])
    r.add_argument("--date", help="Run date YYYY-MM-DD (default today)")
    r.add_argument("--pins-per-product", type=int)
    r.add_argument("--max-products", type=int)
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("track", help="Score results and save lessons for the next run")
    t.add_argument("--niche", required=True)
    t.add_argument("--sheet", required=True, help="Filled-in tracking.csv")
    t.add_argument("--provider", choices=["anthropic", "openai", "ollama", "mock"])
    t.add_argument("--no-lessons", action="store_true")
    t.set_defaults(func=cmd_track)

    a = sub.add_parser("autopilot", help="Write, post and advertise pins unattended (one cycle)")
    a.add_argument("--niche", required=True)
    a.add_argument("--products", help="CSV of candidate products (default: find them on CJ)")
    a.add_argument("--provider", choices=["anthropic", "openai", "ollama", "mock"])
    a.add_argument("--loop", type=float, metavar="HOURS", help="Keep running a cycle every HOURS")
    a.set_defaults(func=cmd_autopilot)

    e = sub.add_parser("economics", help="Check whether ads could be profitable")
    e.add_argument("--conversion", type=float, required=True, help="Share of clicks that buy, e.g. 0.05")
    e.add_argument("--order", type=float, required=True, help="Average order size in dollars")
    e.add_argument("--commission", type=float, required=True, help="Commission rate, e.g. 0.04")
    e.add_argument("--cpc", type=float, help="Ad cost per click to test")
    e.set_defaults(func=cmd_economics)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except (FileNotFoundError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except ImportError as err:
        print(f"Missing package: {err}. Install the extra for your provider, "
              'e.g. pip install -e ".[anthropic]"', file=sys.stderr)
        return 1
    except Exception as err:  # provider errors: bad key, no credit, rate limit
        print(f"{type(err).__name__}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
