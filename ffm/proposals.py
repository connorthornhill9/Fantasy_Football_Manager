"""Proposal model: a single concrete roster move the advisor wants approval for."""
from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel, Field

Kind = Literal["add", "drop", "add_drop", "waiver_claim", "lineup", "ir", "activate_ir", "taxi", "trade"]
KINDS: tuple[str, ...] = ("add", "drop", "add_drop", "waiver_claim", "lineup", "ir", "activate_ir", "taxi", "trade")

KIND_LABELS = {
    "add": "Free agent add",
    "drop": "Drop",
    "add_drop": "Add / drop",
    "waiver_claim": "Waiver claim",
    "lineup": "Lineup change",
    "ir": "Move to IR",
    "activate_ir": "Activate from IR",
    "taxi": "Move to taxi squad",
    "trade": "Trade proposal",
}


class Proposal(BaseModel):
    kind: Kind
    adds: list[str] = Field(default_factory=list)
    drops: list[str] = Field(default_factory=list)
    faab_bid: int | None = None
    starters: list[str] | None = None
    trade_partner_roster_id: int | None = None
    i_give: list[str] = Field(default_factory=list)
    i_get: list[str] = Field(default_factory=list)
    rationale: str
    confidence: Literal["low", "medium", "high"] = "medium"
    priority: int = 1
    expected_gain: str | None = None

    def title(self, names: Callable[[str], str]) -> str:
        """Short one-line title. `names` maps a player id to a display label."""
        if self.kind in ("add", "add_drop", "waiver_claim"):
            parts = []
            if self.adds:
                parts.append("Add " + ", ".join(names(p) for p in self.adds))
            if self.drops:
                parts.append("drop " + ", ".join(names(p) for p in self.drops))
            text = "; ".join(parts)
            if self.kind == "waiver_claim":
                bid = f" (FAAB ${self.faab_bid})" if self.faab_bid is not None else ""
                return f"Waiver claim: {text}{bid}"
            return text
        if self.kind == "drop":
            return "Drop " + ", ".join(names(p) for p in self.drops)
        if self.kind == "lineup":
            return "Set starting lineup"
        if self.kind == "ir":
            return "Move to IR: " + ", ".join(names(p) for p in (self.drops or self.adds))
        if self.kind == "activate_ir":
            return "Activate from IR: " + ", ".join(names(p) for p in (self.adds or self.drops))
        if self.kind == "taxi":
            return "Move to taxi: " + ", ".join(names(p) for p in (self.drops or self.adds))
        if self.kind == "trade":
            give = ", ".join(names(p) for p in self.i_give) or "nothing"
            get = ", ".join(names(p) for p in self.i_get) or "nothing"
            return f"Trade: give {give} for {get}"
        return KIND_LABELS.get(self.kind, self.kind)

    def subject_player_ids(self) -> list[str]:
        return list(dict.fromkeys(self.adds + self.drops + (self.starters or []) + self.i_give + self.i_get))
