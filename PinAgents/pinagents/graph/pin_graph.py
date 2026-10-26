"""Wires the agents together with LangGraph, the same way TradingAgents builds its graph.

    START ─┬─> Product Scout ──┐
           └─> Trend Analyst ──┴─> Pin Writer ─> Compliance ─┬─> Content Manager ─> END
                                       ^                       │
                                       └──── revise rejected ──┘
"""
from datetime import date

from langgraph.graph import END, START, StateGraph

from ..agents.analysts.product_scout import create_product_scout
from ..agents.analysts.trend_analyst import create_trend_analyst
from ..agents.compliance.compliance_checker import create_compliance_checker
from ..agents.creative.pin_writer import create_pin_writer
from ..agents.managers.content_manager import create_content_manager
from ..dataflows.products import load_products
from ..default_config import DEFAULT_CONFIG
from ..llm import LLM
from ..memory import LessonsLog
from ..state import PinState


class PinAgentsGraph:
    def __init__(self, config: dict | None = None, debug: bool = False):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.debug = debug
        self.llm = LLM(self.config)
        self.memory = LessonsLog(self.config["memory_log_path"])
        self.graph = self._build()

    def _build(self):
        g = StateGraph(PinState)
        g.add_node("product_scout", create_product_scout(self.llm, self.config))
        g.add_node("trend_analyst", create_trend_analyst(self.llm, self.config))
        g.add_node("pin_writer", create_pin_writer(self.llm, self.config))
        g.add_node("compliance", create_compliance_checker(self.llm, self.config))
        g.add_node("content_manager", create_content_manager(self.llm, self.config))

        g.add_edge(START, "product_scout")
        g.add_edge(START, "trend_analyst")
        g.add_edge(["product_scout", "trend_analyst"], "pin_writer")
        g.add_edge("pin_writer", "compliance")
        g.add_conditional_edges("compliance", self._after_compliance,
                                {"revise": "pin_writer", "approve": "content_manager"})
        g.add_edge("content_manager", END)
        return g.compile()

    def _after_compliance(self, state) -> str:
        rejected = any(not r["ok"] for r in (state.get("review") or {}).values())
        if rejected and state.get("revision_round", 0) < self.config["max_revision_rounds"]:
            return "revise"
        return "approve"

    def propagate(self, niche: str, products_csv: str, run_date: str | None = None) -> dict:
        state = {
            "niche": niche,
            "run_date": run_date or date.today().isoformat(),
            "products": load_products(products_csv),
            "lessons": self.memory.recent(),
            "revision_round": 0,
            "log": [],
        }
        final_state, shown = state, 0
        for step in self.graph.stream(state, stream_mode="values"):
            log = step.get("log") or []
            if self.debug:
                for line in log[shown:]:
                    print("  -", line)
            shown = len(log)
            final_state = step
        return final_state
