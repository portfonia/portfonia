"""Owner-maintained holding relationship table (#681).

A holding listed with an empty list has been reviewed and has no relations; a
holding missing from the file has not been reviewed yet (the weekly name and
relation check suggests entries for it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

RELATION_TYPES = ("supplier", "customer", "competitor", "input")
MAX_RELATIONS = 8
_PATH = Path(__file__).resolve().parents[2] / "config/instrument_relations.yml"


@dataclass(frozen=True)
class Relation:
    name: str
    aliases: tuple[str, ...]
    relation: str


def load_relations(path: Path | None = None) -> dict[str, list[Relation]]:
    data = yaml.safe_load((path or _PATH).read_text(encoding="utf-8")) or {}
    raw = data.get("relations")
    if not isinstance(raw, dict):
        raise ValueError("relations must be a mapping")
    result: dict[str, list[Relation]] = {}
    for identifier, entries in raw.items():
        entries = entries or []
        if not isinstance(entries, list) or len(entries) > MAX_RELATIONS:
            raise ValueError(f"{identifier}: at most {MAX_RELATIONS} relations")
        relations = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{identifier}: invalid entry")
            name, aliases, relation = entry.get("name"), entry.get("aliases"), entry.get("relation")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(aliases, list)
                or not aliases
                or not all(isinstance(a, str) and a.strip() for a in aliases)
                or relation not in RELATION_TYPES
            ):
                raise ValueError(f"{identifier}: invalid entry")
            relations.append(Relation(name.strip(), tuple(a.strip() for a in aliases), relation))
        result[str(identifier)] = relations
    return result
