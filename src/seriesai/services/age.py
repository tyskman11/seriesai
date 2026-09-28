"""Chronological age calculations kept separate from apparent visual age."""
from __future__ import annotations
from datetime import date

def chronological_age(birth_date: str, at_date: str) -> int:
    born=date.fromisoformat(birth_date); at=date.fromisoformat(at_date)
    if at < born: raise ValueError("Reference date precedes birth date.")
    return at.year-born.year-((at.month,at.day)<(born.month,born.day))
