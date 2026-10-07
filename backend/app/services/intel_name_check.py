"""Weekly name and relation check (#681).

Once a week the alias-gate drops that matched no configured relation are
classified to surface missing names and missing relations, and instruments
with no relation entry get suggested entries. Nothing is written to the
configuration files; the batch report carries ready-to-paste YAML for a PR.
"""

from __future__ import annotations

from datetime import date

import httpx
import yaml
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import InstrumentProfile
from app.services.headline_cleaning import classifier_error, openrouter_json
from app.services.instrument_news_sources import CollectedItem, rows
from app.services.instrument_relations import MAX_RELATIONS, RELATION_TYPES, Relation
from app.services.instrument_universe import UniverseEntry

MIN_RELATION_COUNT = 2
SAMPLES = 2

GAP_PROMPT = 'You review financial news items that were fetched for one company but do not name it with any of its listed names.\nFor each item return one label about THIS company:\nnamed = the item is actually about this company, using a name, product, subsidiary or ticker the list is missing; also return "name": the exact words in the title that refer to the company;\nindirect = a concrete development about another company that materially affects THIS company as its supplier, customer, direct competitor or key input; also return "entity": that company\'s name, and "relation": supplier|customer|competitor|input (the other company\'s role for THIS company);\ngeneric = broad market, macro or multi-sector piece with no specific link to this company;\npromo = stock picks, listicles, comparisons, predictions or sponsored content;\nunrelated = nothing to do with this company.\nUse only well-established business relationships; do not invent links. Judge only from the words given.\nOutput ONLY JSON: {"labels": [{"id": int, "label": "named|indirect|generic|promo|unrelated", "name": str|null, "entity": str|null, "relation": str|null}]}'

SUGGEST_PROMPT = 'You maintain a news-matching table for one listed company.\nReturn extra aliases (short names, subsidiaries or products that unambiguously mean this company) and at most 8 well-established related companies (its key suppliers, customers, direct competitors or key input producers).\nNever use executive names, common surnames or ordinary words as aliases. Do not repeat names already listed. Only include relationships that are widely documented.\nOutput ONLY JSON: {"aliases": [str], "relations": [{"name": str, "aliases": [str], "relation": "supplier|customer|competitor|input"}]}'


def weekly_due(slot: str, run_date: date) -> bool:
    return slot == "pre_open" and run_date.weekday() == 6


def run_weekly_check(
    session: Session,
    unmatched: dict[str, list[CollectedItem]],
    universe: list[UniverseEntry],
    relations: dict[str, list[Relation]],
) -> dict[str, object]:
    profiles = {p.identifier: p for p in session.query(InstrumentProfile).all()}
    size = min(100, max(1, get_settings().INTEL_CLASSIFIER_BATCH))
    errors: list[str] = []
    calls = checked = 0
    cost = 0.0
    names: dict[tuple[str, str], list[str]] = {}
    gaps: dict[tuple[str, str, str], list[str]] = {}
    for identifier, items in unmatched.items():
        profile = profiles.get(identifier)
        aliases = list(profile.aliases) if profile and profile.aliases else [identifier]
        known = {a.casefold() for a in aliases}
        related = {
            a.casefold() for r in relations.get(identifier, []) for a in (r.name, *r.aliases)
        }
        for start in range(0, len(items), size):
            chunk = items[start : start + size]
            content = f"Company: {identifier} ({', '.join(aliases)})\n" + "\n".join(
                f"{i}\t{x.title}\t{(x.summary or '')[:200]}" for i, x in enumerate(chunk)
            )
            calls += 1
            try:
                data, spent = openrouter_json(GAP_PROMPT, content)
                labelled = rows(data["labels"])
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append(classifier_error(exc))
                continue
            cost += spent
            checked += len(chunk)
            for row in labelled:
                idx = row.get("id")
                if not isinstance(idx, int) or not 0 <= idx < len(chunk):
                    continue
                title = chunk[idx].title
                if row.get("label") == "named":
                    name = str(row.get("name") or "").strip()
                    if name and name.casefold() not in known:
                        names.setdefault((identifier, name), []).append(title)
                elif row.get("label") == "indirect":
                    entity = str(row.get("entity") or "").strip()
                    relation = str(row.get("relation") or "")
                    if (
                        entity
                        and relation in RELATION_TYPES
                        and entity.casefold() not in related | known
                    ):
                        gaps.setdefault((identifier, entity, relation), []).append(title)
    unreviewed = [e.identifier for e in universe if e.identifier not in relations]
    suggestions: dict[str, dict[str, object]] = {}
    for identifier in unreviewed:
        profile = profiles.get(identifier)
        listed = [n for n in (profile.name_en, profile.name_zh) if n] if profile else []
        entry = next(e for e in universe if e.identifier == identifier)
        content = f"Ticker: {entry.ticker}; market: {entry.market}; names: {', '.join(listed) or 'unknown'}"
        calls += 1
        try:
            data, spent = openrouter_json(SUGGEST_PROMPT, content)
            cost += spent
            suggestions[identifier] = _suggestion(data)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append(classifier_error(exc))
    return {
        "checked": checked,
        "calls": calls,
        "cost_usd": round(cost, 6),
        "names": [
            {"identifier": i, "name": n, "count": len(t), "samples": t[:SAMPLES]}
            for (i, n), t in sorted(names.items(), key=lambda p: (p[0][0], -len(p[1]), p[0][1]))
        ],
        "relations": [
            {"identifier": i, "entity": e, "relation": r, "count": len(t), "samples": t[:SAMPLES]}
            for (i, e, r), t in sorted(gaps.items(), key=lambda p: (p[0][0], -len(p[1]), p[0][1]))
            if len(t) >= MIN_RELATION_COUNT
        ],
        "unreviewed": unreviewed,
        "suggestions": suggestions,
        "errors": errors,
    }


def _suggestion(data: dict[str, object]) -> dict[str, object]:
    aliases = [str(a).strip() for a in rows_or_list(data.get("aliases")) if str(a).strip()]
    relations = []
    for row in rows(data.get("relations") or []):
        name = str(row.get("name") or "").strip()
        names = [str(a).strip() for a in rows_or_list(row.get("aliases")) if str(a).strip()]
        if name and row.get("relation") in RELATION_TYPES:
            relations.append(
                {"name": name, "aliases": names or [name], "relation": str(row["relation"])}
            )
    return {"aliases": aliases, "relations": relations[:MAX_RELATIONS]}


def rows_or_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def render_weekly(details: dict[str, object]) -> list[str]:
    from app.services.intel_digest import errors_from, number, objects, problem_lines

    lines = [
        "PART 3 - WEEKLY NAME AND RELATION CHECK",
        f"Checked {number(details.get('checked')):g} dropped headlines in "
        f"{number(details.get('calls')):g} AI calls, cost ${number(details.get('cost_usd')):.3f}.",
    ]
    names = objects(details.get("names"))
    gaps = objects(details.get("relations"))
    lines.append("Possible missing names:" if names else "Possible missing names: none")
    for row in names:
        lines.append(
            f'  {row["identifier"]}: "{row["name"]}" ({number(row.get("count")):g} headlines)'
        )
        lines += _samples(row)
    lines.append("Possible missing relations:" if gaps else "Possible missing relations: none")
    for row in gaps:
        lines.append(
            f"  {row['identifier']}: {row['entity']}, {row['relation']} ({number(row.get('count')):g} headlines)"
        )
        lines += _samples(row)
    unreviewed = [v for v in rows_or_list(details.get("unreviewed")) if isinstance(v, str)]
    lines.append(
        "Instruments without a relation entry: " + (", ".join(unreviewed) if unreviewed else "none")
    )
    aliases: dict[str, list[str]] = {}
    relations: dict[str, list[dict[str, object]]] = {}
    for row in names:
        aliases.setdefault(str(row["identifier"]), []).append(str(row["name"]))
    for row in gaps:
        relations.setdefault(str(row["identifier"]), []).append(
            _relation_document(str(row["entity"]), [str(row["entity"])], str(row["relation"]))
        )
    raw = details.get("suggestions")
    for identifier, suggestion in raw.items() if isinstance(raw, dict) else []:
        if not isinstance(suggestion, dict):
            continue
        aliases.setdefault(str(identifier), []).extend(
            str(a) for a in rows_or_list(suggestion.get("aliases"))
        )
        relations.setdefault(str(identifier), [])
        for rel in objects(suggestion.get("relations")):
            relations[str(identifier)].append(
                _relation_document(
                    str(rel["name"]),
                    [str(a) for a in rows_or_list(rel.get("aliases"))],
                    str(rel["relation"]),
                )
            )
    if any(aliases.values()) or relations:
        lines.append("Suggested YAML (review before adding through a PR):")
        doc: dict[str, object] = {}
        if any(aliases.values()):
            doc["entity_aliases"] = {
                identifier: list(dict.fromkeys(values))
                for identifier, values in aliases.items()
                if values
            }
        if relations:
            doc["relations"] = relations
        lines += [
            "  " + line
            for line in yaml.safe_dump(
                doc, allow_unicode=True, sort_keys=False, default_flow_style=None, width=1000
            ).splitlines()
        ]
    return [*lines, *problem_lines(errors_from(details.get("errors")))]


def _samples(row: dict[str, object]) -> list[str]:
    samples = [s for s in rows_or_list(row.get("samples")) if isinstance(s, str)]
    return ["      e.g. " + "; ".join(f'"{s}"' for s in samples)] if samples else []


def _relation_document(name: str, aliases: list[str], relation: str) -> dict[str, object]:
    return {"name": name, "aliases": aliases or [name], "relation": relation}
