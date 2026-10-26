"""Default settings. Copy this dict and change what you need, like TradingAgents' DEFAULT_CONFIG."""
import os

DEFAULT_CONFIG = {
    # LLM provider: "anthropic", "openai", "ollama", or "mock" (offline, no key, for testing)
    "llm_provider": os.getenv("PINAGENTS_LLM_PROVIDER", "mock"),
    # Quick model: scout, trend analyst, pin writer. Deep model: compliance review, manager, lessons.
    "quick_think_llm": os.getenv("PINAGENTS_QUICK_LLM", ""),
    "deep_think_llm": os.getenv("PINAGENTS_DEEP_LLM", ""),
    "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),

    # Content
    "max_products": 5,          # products the scout shortlists per run
    "pins_per_product": 2,      # pin variations written per product
    "max_revision_rounds": 2,   # writer <-> compliance loops before rejected pins are dropped
    "pins_per_day": 3,          # posting schedule pace
    "posting_times": ["08:00", "13:00", "20:00"],
    "disclosure": "Affiliate link: I may earn a commission if you buy.",

    # Files
    "results_dir": os.getenv("PINAGENTS_RESULTS_DIR", "results"),
    "memory_log_path": os.getenv(
        "PINAGENTS_MEMORY_LOG", os.path.expanduser("~/.pinagents/lessons.md")
    ),

    # Stage 2 (posting through the official Pinterest API). Off until you have Standard access.
    "allow_posting": os.getenv("PINAGENTS_ALLOW_POSTING") == "1",
    "pinterest_board_id": os.getenv("PINTEREST_BOARD_ID", ""),
    "pinterest_token_path": os.path.expanduser("~/.pinagents/pinterest_token.json"),

    # Stage 3 (ads). Hard caps enforced in code by BudgetGuard, never by the LLM.
    "allow_ads": os.getenv("PINAGENTS_ALLOW_ADS") == "1",
    "pinterest_ad_account_id": os.getenv("PINTEREST_AD_ACCOUNT_ID", ""),
    "ads_daily_cap": float(os.getenv("PINAGENTS_ADS_DAILY_CAP", "5.00")),
    "ads_total_cap": float(os.getenv("PINAGENTS_ADS_TOTAL_CAP", "100.00")),
    "ads_ledger_path": os.path.expanduser("~/.pinagents/ad_spend.json"),
    "kill_switch_path": os.path.expanduser("~/.pinagents/STOP"),

    # Autopilot (unattended posting + ads)
    "autopilot_state_path": os.path.expanduser("~/.pinagents/autopilot.json"),
    "ad_group_daily_budget": 1.00,      # per product; daily cap / this = products advertised at once
    "ads_min_bid": 0.10,                # products that can't afford this per click are never advertised
    "ads_test_spend_per_product": 20.00,  # a product that hasn't proven profitable by then is paused
    # Share of clicks that buy, used to cap bids. Deliberately low: experiments/simulate.py found
    # that cautious bids buy cheaper clicks and beat optimistic ones across all its scenarios.
    "assumed_conversion_rate": 0.01,
    "ads_countries": ["US"],
    # CSV of product_id,earnings from your affiliate network (cumulative, from ad links only).
    "earnings_csv": os.getenv("PINAGENTS_EARNINGS_CSV", ""),
}

DEFAULT_MODELS = {
    "anthropic": {"quick": "claude-haiku-5-5", "deep": "claude-sonnet-5-5"},
    "openai": {"quick": "gpt-6-luna", "deep": "gpt-6-sol"},
    "ollama": {"quick": "llama3.1", "deep": "llama3.1"},
    "mock": {"quick": "mock", "deep": "mock"},
}
