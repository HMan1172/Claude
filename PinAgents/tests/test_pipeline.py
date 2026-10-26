import csv
import os

import pytest

from pinagents.agents.compliance.compliance_checker import hard_checks
from pinagents.budget_guard import BudgetGuard, SpendRefused
from pinagents.economics import expected_epc, max_cpc, measured_epc
from pinagents.graph.pin_graph import PinAgentsGraph
from pinagents.llm import LLM, _extract_json
from pinagents.output import save_run
from pinagents.tracker import analyze

ROOT = os.path.dirname(os.path.dirname(__file__))
PRODUCTS = os.path.join(ROOT, "products_example.csv")
DISCLOSURE = "Affiliate link: I may earn a commission if you buy."


def cfg(tmp_path, **kw):
    return {"llm_provider": "mock", "results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "lessons.md"), **kw}


def good_pin(**kw):
    pin = {"title": "A tidy desk setup", "description": f"{DISCLOSURE} Keeps cables off the floor.",
           "overlay_text": "Tidy desk", "link": "https://www.example.com/x?tag=me"}
    pin.update(kw)
    return pin


def test_full_run_offline(tmp_path):
    c = cfg(tmp_path)
    state = PinAgentsGraph(c).propagate("home office", PRODUCTS, run_date="2026-10-25")
    pins = state["final"]["pins"]
    assert len(pins) == 10  # 5 products x 2 variants
    assert all(p["description"].startswith(DISCLOSURE) for p in pins)
    assert pins[0]["post_date"] == "2026-10-26"
    run_dir = save_run(state, c)
    for name in ["pins.csv", "tracking.csv", "report.html", "state.json"]:
        assert os.path.exists(os.path.join(run_dir, name))
    assert len(os.listdir(os.path.join(run_dir, "images"))) == 10


def test_bad_links_never_get_scheduled(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("id,name,url,price,commission_rate\n"
                   "a,Good Thing,https://www.example.com/a,20,5%\n"
                   "b,Short Link,https://bit.ly/abc,20,5%\n"
                   "c,Plain Http,http://www.example.com/c,20,5%\n")
    state = PinAgentsGraph(cfg(tmp_path)).propagate("test", str(bad), run_date="2026-10-25")
    scheduled = {p["product_id"] for p in state["final"]["pins"]}
    assert scheduled == {"a"}
    assert state["revision_round"] == 2  # it tried to fix them, then gave up and dropped them
    assert len(state["final"]["dropped"]) == 4


def test_hard_checks():
    assert hard_checks(good_pin(), DISCLOSURE) == []
    assert hard_checks(good_pin(description="No disclosure"), DISCLOSURE)
    assert hard_checks(good_pin(title="x" * 101), DISCLOSURE)
    assert hard_checks(good_pin(link="https://tinyurl.com/x"), DISCLOSURE)
    assert hard_checks(good_pin(description=f"{DISCLOSURE} This cures back pain."), DISCLOSURE)
    assert hard_checks(good_pin(description=f"{DISCLOSURE} I bought this last year."), DISCLOSURE)


def test_budget_guard(tmp_path):
    g = BudgetGuard(5, 12, str(tmp_path / "ledger.json"), str(tmp_path / "STOP"))
    g.authorize(3, today="2026-10-25")
    g.authorize(2, today="2026-10-25")
    with pytest.raises(SpendRefused):
        g.authorize(0.01, today="2026-10-25")          # daily cap
    g.authorize(5, today="2026-10-26")
    with pytest.raises(SpendRefused):
        g.authorize(3, today="2026-10-27")             # total cap (10 + 3 > 12)
    (tmp_path / "STOP").write_text("")
    with pytest.raises(SpendRefused):
        g.authorize(1, today="2026-10-28")             # kill switch


def test_economics():
    assert expected_epc(0.05, 40, 0.04) == pytest.approx(0.08)
    assert measured_epc(10, 0) == 0
    assert max_cpc(0.10) == pytest.approx(0.07)


def test_tracker_writes_lessons(tmp_path):
    sheet = tmp_path / "tracking.csv"
    with open(sheet, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pin_key", "product_id", "title", "posted_date", "impressions",
                    "outbound_clicks", "saves", "earnings"])
        w.writerow(["a-v1", "a", "A", "2026-10-26", "5000", "40", "3", "$6.00"])
        w.writerow(["b-v1", "b", "B", "2026-10-26", "3000", "25", "1", "0.50"])
        w.writerow(["c-v1", "c", "C", "", "", "", "", ""])  # not posted yet
    c = cfg(tmp_path)
    s = analyze(str(sheet), "test", LLM(c), c)
    assert s["pins"] == 2 and s["clicks"] == 65
    assert s["epc"] == pytest.approx(6.5 / 65)
    assert s["best"][0]["key"] == "a-v1"
    assert "Totals" in (tmp_path / "lessons.md").read_text()


def test_extract_json():
    assert _extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('here: [1, 2] done') == [1, 2]


class StubLLM:
    def __init__(self, reply):
        self.reply = reply

    def json(self, *args, **kwargs):
        return self.reply


def test_scout_never_shortlists_a_product_twice():
    from pinagents.agents.analysts.product_scout import create_product_scout
    products = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    llm = StubLLM({"ranked": [{"id": "a", "score": 9}, {"id": "a", "score": 8}, {"id": "b", "score": 7}]})
    out = create_product_scout(llm, {"max_products": 5})({"products": products, "niche": "x"})
    assert [p["id"] for p in out["shortlist"]] == ["a", "b"]


def test_writer_keeps_rejected_pins_the_model_skipped():
    from pinagents.agents.creative.pin_writer import create_pin_writer
    product = {"id": "a", "name": "A", "url": "https://www.example.com/a"}
    drafts = [{"key": "a-v1", "product_id": "a"}, {"key": "a-v2", "product_id": "a"}]
    review = {"a-v1": {"ok": False, "issues": ["x"]}, "a-v2": {"ok": False, "issues": ["y"]}}
    llm = StubLLM({"pins": [{"product_id": "a", "variant": 1, "title": "New", "description": "d"}]})
    out = create_pin_writer(llm, {"disclosure": DISCLOSURE})(
        {"shortlist": [product], "drafts": drafts, "review": review, "niche": "x"})
    assert sorted(d["key"] for d in out["drafts"]) == ["a-v1", "a-v2"]
