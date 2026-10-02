"""Clean original article text before deciding whether it is usable."""

import re
from urllib.parse import urlsplit

from app.services.intel_deepen_config import DeepenConfig


def without_urls(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^\n]*?\)", r"\1", text)
    text = re.sub(r"https?://[^\s<>\])]+", "", text, flags=re.IGNORECASE)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()).strip()


def clean_body(url: str, text: str, cfg: DeepenConfig) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "finance.yahoo.com" or host.endswith(".finance.yahoo.com"):
        had_navigation = any(m.casefold() in text.casefold() for m in cfg.yahoo_nav_markers)
        if "[...]" in text:
            text = text.split("[...]", 1)[1]
        text = "\n".join(
            line
            for line in text.splitlines()
            if not any(m.casefold() in line.casefold() for m in cfg.yahoo_nav_markers)
        )
        events = r"\b(awarded|agreement|contract|delivery|delivered|order|guidance|lawsuit|regulator|financing)\b"
        if (
            had_navigation
            and len(text.strip()) < 600
            and not re.search(events, text, re.IGNORECASE)
        ):
            text = ""
    return without_urls(text)[: cfg.extract.body_max_chars]


def body_verdict(text: str, cfg: DeepenConfig) -> tuple[bool, str | None]:
    if not text.strip():
        return False, "empty"
    if len(text) < cfg.extract.min_body_chars:
        return False, "too_short"
    if any(m.casefold() in text.casefold() for m in cfg.paywall_markers):
        return False, "paywall"
    lines = [line for line in text.splitlines() if line.strip()]
    total = sum(map(len, lines))
    boilerplate = sum(
        len(line)
        for line in lines
        if any(m.casefold() in line.casefold() for m in cfg.boilerplate_markers)
    )
    if total and boilerplate / total > cfg.extract.max_boilerplate_ratio:
        return False, "boilerplate"
    return True, None
