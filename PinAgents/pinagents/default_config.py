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
    "allow_posting": False,
    "pinterest_board_id": os.getenv("PINTEREST_BOARD_ID", ""),

    # Stage 3 (ads). Hard caps enforced in code by BudgetGuard, never by the LLM.
    "ads_daily_cap": 5.00,
    "ads_total_cap": 100.00,
    "ads_ledger_path": os.path.expanduser("~/.pinagents/ad_spend.json"),
    "kill_switch_path": os.path.expanduser("~/.pinagents/STOP"),
}

DEFAULT_MODELS = {
    "anthropic": {"quick": "claude-haiku-5-5", "deep": "claude-sonnet-5-5"},
    "openai": {"quick": "gpt-6-luna", "deep": "gpt-6-sol"},
    "ollama": {"quick": "llama3.1", "deep": "llama3.1"},
    "mock": {"quick": "mock", "deep": "mock"},
}
