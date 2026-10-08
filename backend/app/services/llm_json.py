"""
Shared, provider-agnostic helper for extracting and parsing JSON that an LLM
was asked to output.

Ported from the Homepage canonical module `llm_json_utils.py` (issue #700):
the production container cannot read another repository, so this copy keeps
the upstream logic and docstrings unchanged. Deliberate differences are type
annotations for `mypy --strict` and a module-level `repair_json` import,
because `json-repair` is a declared dependency here (`requirements.txt`) and
the upstream tests patch the module-level name.

Why this exists: LLMs are sampled token-by-token, not constrained by a JSON
grammar unless the caller uses a provider-specific structured-output/tool-use
mode. Plain "please output this JSON" prompting is a soft constraint, so
malformed output is expected at some rate — most commonly an unescaped quote
inside a free-text field (e.g. quoting a source document), a dangling comma,
a literal newline inside a string, or truncation from too small a max_tokens.
Retrying the whole paid LLM call for a locally-repairable formatting slip
wastes money; this module tries a local repair first.
"""

import json
import logging
from typing import Any

from json_repair import repair_json

# Hard cap on how many candidate bracket positions _find_balanced_json will
# try. Without this, a degenerate run of bracket characters (e.g. a model
# stuck in a repetition loop emitting "[[[[[[...") makes every position a
# candidate whose raw_decode attempt scans to end-of-string before failing —
# O(n^2) on adversarial input, measured at several seconds for ~16K
# characters and minutes for realistic 50-100KB LLM output.
_MAX_CANDIDATE_ATTEMPTS = 100


def _find_balanced_json(content: str) -> str:
    """Scan for the first complete top-level JSON object or array.

    Tries each candidate `{`/`[` position in turn via
    `json.JSONDecoder.raw_decode`, which parses one JSON value starting at
    a given index and tolerates trailing text after it. A false start
    (e.g. a brace that's actually inside quoted prose) raises
    JSONDecodeError, so the loop just moves on to the next candidate
    bracket rather than getting stuck on it.

    Among all candidates that parse successfully, keeps the *longest* one
    rather than the first: short incidental fragments that happen to be
    valid JSON on their own (an empty `{}` example, a `[1]` citation
    marker) are common in LLM prose ahead of the real answer, and the real
    answer is virtually always the larger structure. Known limitation:
    this heuristic inverts if the real answer is genuinely short and a
    larger incidental JSON-shaped fragment (e.g. a schema example) appears
    elsewhere in the text — there's no way to tell "the answer" from "an
    example" without understanding the prose, so this trades one failure
    mode for a rarer one rather than eliminating the ambiguity.

    If no candidate parses cleanly, falls back to slicing from the
    earliest candidate whose failure ran off the end of content (genuine
    truncation, not a wrong start — see the `e.pos` check below), so
    repair_json gets a fair shot at fixing truncation without also having
    to untangle a false start that happened to precede it. If there's no
    such candidate either, falls back to the stripped input, so callers
    relying on error propagation from json.loads/json_repair still see a
    sensible failure.
    """
    decoder = json.JSONDecoder()
    truncated_start = None  # first candidate whose failure ran off the end
    best_span = None  # (start, end) of the longest successful parse so far
    search_from = 0
    attempts = 0

    while attempts < _MAX_CANDIDATE_ATTEMPTS:
        start = None
        for i in range(search_from, len(content)):
            if content[i] in "{[":
                start = i
                break
        if start is None:
            break
        attempts += 1
        try:
            _, end = decoder.raw_decode(content, start)
            if best_span is None or (end - start) > (best_span[1] - best_span[0]):
                best_span = (start, end)
        except json.JSONDecodeError as e:
            # A false start (e.g. a brace inside quoted prose) fails with
            # the error position somewhere *before* the end of content —
            # that's a structurally wrong start, not a fair fallback
            # candidate. Genuine truncation (ran out of input mid-value)
            # fails with the error position at the very end of content.
            # Only the latter is worth falling back to.
            if truncated_start is None and e.pos == len(content):
                truncated_start = start
        search_from = start + 1

    if best_span is not None:
        return content[best_span[0] : best_span[1]]
    if truncated_start is not None:
        return content[truncated_start:]
    return content.strip()


def extract_json_blob(content: str) -> str:
    """Pull the JSON value (object or array) out of raw LLM output, stripping
    any markdown code fence markers and stray prose before/after the JSON.

    No fence-specific regex is used to locate the JSON — backtick
    characters aren't `{`/`[`, so a fence never registers as a candidate
    bracket and `_find_balanced_json` naturally ignores it as
    surrounding text, whether it wraps the real JSON or appears elsewhere
    in the content (e.g. an unrelated illustrative code snippet after an
    unfenced JSON answer). An earlier version tried to strip the fence
    markers explicitly via regex; that turned out to be unnecessary and,
    when the fence was unrelated to the actual JSON, actively harmful (it
    discarded a real unfenced answer that appeared before the fence)."""
    return _find_balanced_json(content)


def parse_llm_json(content: str, logger: logging.Logger | None = None) -> Any:  # any JSON type
    """Extract and parse a JSON value from raw LLM output. Tries a strict
    parse first; on failure, attempts a local repair (json_repair) before
    raising, so a recoverable formatting slip doesn't force a re-call of the
    (paid) model. Raises json.JSONDecodeError if repair also fails.

    Return type is `Any`, not `dict`: every consumer of this module today
    asks the LLM for a JSON object, but json.loads (on the direct-parse
    path) and json_repair (on the repair path) can both legitimately hand
    back any JSON type — a bare top-level scalar/array is valid JSON even
    though no current caller sends a prompt that produces one.

    `logger` is optional — pass a stdlib Logger to get a warning when repair
    was needed, so recurring failure patterns stay visible without forcing
    every caller to add its own logging.
    """
    json_str = extract_json_blob(content)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        repaired = repair_json(json_str)
        # repair_json returns a JSON string by default; only re-parse if it
        # actually gave us a string back. A future version returning an
        # already-parsed object (of *any* JSON type, not just dict/list)
        # must not be re-parsed — json.loads on a non-str raises TypeError,
        # not JSONDecodeError, which callers that only catch the latter
        # would miss.
        result = json.loads(repaired) if isinstance(repaired, str) else repaired
        if logger:
            logger.warning(f"parse_llm_json: repaired malformed JSON (original error: {e})")
        return result
