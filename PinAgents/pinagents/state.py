"""The shared state that flows through the agent graph."""
import operator
from typing import Annotated, TypedDict


class PinState(TypedDict, total=False):
    niche: str
    run_date: str
    products: list[dict]        # loaded from the products CSV
    lessons: str                # from the memory log of earlier runs
    shortlist: list[dict]       # product scout output
    trends: dict                # trend analyst output
    drafts: list[dict]          # pin writer output
    review: dict                # compliance results keyed by pin key
    revision_round: int
    final: dict                 # content manager output: approved pins + schedule
    log: Annotated[list[str], operator.add]
