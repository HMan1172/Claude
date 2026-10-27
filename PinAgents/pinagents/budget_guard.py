"""Hard spending limits for the ads stage. Plain code, outside the AI's control.

Any agent that wants to spend must call guard.authorize(amount) first. It refuses if:
  - the kill switch file exists (create ~/.pinagents/STOP to halt all spending),
  - today's spend would pass the daily cap,
  - total spend would pass the total cap.
"""
import json
import os
from datetime import date


class SpendRefused(Exception):
    pass


class BudgetGuard:
    def __init__(self, daily_cap: float, total_cap: float, ledger_path: str, kill_switch_path: str):
        if daily_cap <= 0 or total_cap <= 0:
            raise ValueError("Caps must be positive.")
        self.daily_cap = daily_cap
        self.total_cap = total_cap
        self.ledger_path = ledger_path
        self.kill_switch_path = kill_switch_path

    @classmethod
    def from_config(cls, config: dict) -> "BudgetGuard":
        return cls(config["ads_daily_cap"], config["ads_total_cap"],
                   config["ads_ledger_path"], config["kill_switch_path"])

    def _ledger(self) -> dict:
        if not os.path.exists(self.ledger_path):
            return {"total": 0.0, "days": {}}
        with open(self.ledger_path, encoding="utf-8") as f:
            return json.load(f)

    def spent_today(self, today: str | None = None) -> float:
        return self._ledger()["days"].get(today or date.today().isoformat(), 0.0)

    def spent_total(self) -> float:
        return self._ledger()["total"]

    def authorize(self, amount: float, today: str | None = None) -> None:
        """Record the spend if allowed, otherwise raise SpendRefused."""
        if amount <= 0:
            raise SpendRefused("Amount must be positive.")
        if os.path.exists(self.kill_switch_path):
            raise SpendRefused(f"Kill switch is on ({self.kill_switch_path}).")
        today = today or date.today().isoformat()
        ledger = self._ledger()
        day_spent = ledger["days"].get(today, 0.0)
        if day_spent + amount > self.daily_cap + 1e-9:
            raise SpendRefused(f"Daily cap ${self.daily_cap:.2f} reached (spent ${day_spent:.2f}).")
        if ledger["total"] + amount > self.total_cap + 1e-9:
            raise SpendRefused(f"Total cap ${self.total_cap:.2f} reached (spent ${ledger['total']:.2f}).")
        ledger["days"][today] = round(day_spent + amount, 2)
        ledger["total"] = round(ledger["total"] + amount, 2)
        self._save(ledger)

    def settle(self, day: str, actual: float) -> None:
        """Replace what was authorized for a finished day with what was really spent."""
        ledger = self._ledger()
        ledger["total"] = round(ledger["total"] - ledger["days"].get(day, 0.0) + actual, 2)
        ledger["days"][day] = round(actual, 2)
        self._save(ledger)

    def _save(self, ledger: dict) -> None:
        write_json(self.ledger_path, ledger)


def write_json(path: str, data) -> None:
    """Write to a temporary file, then rename over the target, so a crash mid-write can never leave
    a half-written file behind (a corrupt ledger would stop every later cycle)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
