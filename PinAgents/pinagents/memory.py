"""Lessons log: what earned and what didn't, carried into every later run (like TradingAgents' memory log)."""
import os
from datetime import date


class LessonsLog:
    def __init__(self, path: str):
        self.path = path

    def recent(self, max_chars: int = 3000) -> str:
        if not os.path.exists(self.path):
            return ""
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        return text[-max_chars:]

    def append(self, niche: str, lines: list[str]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"\n## {date.today().isoformat()} - {niche}\n")
            for line in lines:
                f.write(line.rstrip() + "\n")
