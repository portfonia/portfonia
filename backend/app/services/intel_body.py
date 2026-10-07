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
    return strip_residue(without_urls(text), cfg)[: cfg.extract.body_max_chars]


_TLD = r"(?:com|net|org|co|io|news|info|biz|uk|sg|hk|cn|jp|de|fr|eu|au|ca|in)"
_HOST = r"(?:[A-Za-z0-9-]+\.)+" + _TLD
BARE_DOMAIN_LINE = re.compile(r"^(?:[^|]{0,40}\|\s*)?" + _HOST + r"(?:/\S*)?$", re.IGNORECASE)
DOMAIN_WITH_PATH = re.compile(r"\b" + _HOST + r"/\S*", re.IGNORECASE)


def strip_residue(text: str, cfg: DeepenConfig) -> str:
    rules = cfg.body_cleaning

    def is_para(line: str) -> bool:
        if line.startswith("|"):
            return False
        words = len(line.split())
        terminators = len(re.findall(r'[.!?]["\u201d\u2019)]?(?=\s|$)', line))
        english = (
            words >= rules.paragraph_min_words
            and bool(re.search(r'[.!?"\u201d\u2019)]$|\. ', line))
            and words / max(1, terminators) <= rules.max_words_per_sentence
        )
        cjk = len(
            re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", line)
        ) >= rules.paragraph_min_cjk_chars and bool(re.search(r"[\u3002\uff01\uff1f]", line))
        return english or cjk

    out: list[str] = []
    skipping = False
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if BARE_DOMAIN_LINE.fullmatch(line):
            continue
        paragraph = is_para(line)
        if not paragraph and rules.section(line):
            skipping = True
            continue
        if skipping and not paragraph:
            continue
        skipping = False
        if rules.residue(line, paragraph):
            continue
        out.append(line)
    paras = [i for i, line in enumerate(out) if is_para(line)]
    if not paras:
        return ""
    title = next((line for line in reversed(out[: paras[0]]) if line.startswith("# ")), None)
    body = [
        line
        for line in out[paras[0] : paras[-1] + 1]
        if not re.match(r"^\d+\.\s+(\d+\.\s+)?\S.{0,40}$", line)
    ]
    result = "\n".join(([title] if title and title not in body else []) + body)
    return DOMAIN_WITH_PATH.sub("", result)


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
