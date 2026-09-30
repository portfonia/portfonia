"""Convert Simplified Chinese to Taiwan Traditional with financial term overrides."""

import re
from functools import lru_cache
from pathlib import Path
from typing import cast

import yaml
from opencc import OpenCC
from yaml.nodes import MappingNode, ScalarNode

_TERMS_FILE = Path(__file__).resolve().parents[2] / "config" / "zh_hant_terms.yml"
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")


def load_terms(path: Path = _TERMS_FILE) -> dict[str, str]:
    """Read a flat mapping and reject duplicate keys or non-string/empty terms."""
    root = yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
    if not isinstance(root, MappingNode):
        raise ValueError("Traditional term overrides must be a flat mapping")
    terms: dict[str, str] = {}
    for key, value in root.value:
        for node in (key, value):
            if (
                not isinstance(node, ScalarNode)
                or node.tag != "tag:yaml.org,2002:str"
                or not node.value.strip()
            ):
                raise ValueError("Traditional term overrides require non-empty strings")
        if key.value in terms:
            raise ValueError(f"Duplicate Traditional override key: {key.value!r}")
        terms[key.value] = value.value
    return terms


@lru_cache(maxsize=1)
def _converter() -> OpenCC:
    return OpenCC("s2twp")


def to_traditional(text: str) -> str:
    """Preserve non-CJK text; emit override targets verbatim before OpenCC conversion."""
    if not _CJK.search(text):
        return text
    terms = load_terms()
    if not terms:
        return cast(str, _converter().convert(text))
    pattern = re.compile(
        "(" + "|".join(re.escape(k) for k in sorted(terms, key=len, reverse=True)) + ")"
    )
    segments = pattern.split(text)
    return "".join(
        terms[segment] if index % 2 else cast(str, _converter().convert(segment))
        for index, segment in enumerate(segments)
    )
