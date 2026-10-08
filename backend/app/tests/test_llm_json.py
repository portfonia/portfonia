"""Unit tests for llm_json, ported from Homepage test_llm_json_utils.py (#700)."""

import json
import logging
import time
import unittest
from unittest.mock import patch

from app.services import llm_json
from app.services.llm_json import extract_json_blob, parse_llm_json


class ExtractJsonBlobTests(unittest.TestCase):
    def test_plain_object(self) -> None:
        self.assertEqual(extract_json_blob('{"ok": true}'), '{"ok": true}')

    def test_nested_object(self) -> None:
        raw = '{"a": {"b": [1, 2, {"c": 3}]}}'
        self.assertEqual(extract_json_blob(raw), raw)

    def test_fenced_code_block(self) -> None:
        raw = '```json\n{"ok": true}\n```'
        self.assertEqual(extract_json_blob(raw), '{"ok": true}')

    def test_fenced_code_block_no_lang_tag(self) -> None:
        raw = '```\n{"ok": true}\n```'
        self.assertEqual(extract_json_blob(raw), '{"ok": true}')

    def test_trailing_prose_with_braces(self) -> None:
        raw = 'Result: {"ok": true} but note {not json}'
        self.assertEqual(extract_json_blob(raw), '{"ok": true}')

    def test_leading_prose(self) -> None:
        raw = 'Here is the answer: {"ok": true}'
        self.assertEqual(extract_json_blob(raw), '{"ok": true}')

    def test_escaped_quote_in_string_does_not_break_scan(self) -> None:
        raw = r'{"note": "she said \"hi {not a brace}\""} trailing junk {x}'
        extracted = extract_json_blob(raw)
        self.assertEqual(extracted, r'{"note": "she said \"hi {not a brace}\""}')
        self.assertEqual(json.loads(extracted), {"note": 'she said "hi {not a brace}"'})

    def test_top_level_array(self) -> None:
        raw = '[{"a": 1}, {"b": 2}] trailing prose'
        self.assertEqual(extract_json_blob(raw), '[{"a": 1}, {"b": 2}]')

    def test_brace_inside_string_before_object(self) -> None:
        # Regression test for a limitation of the earlier hand-rolled
        # bracket/string scanner: it anchored on the first '{' it saw,
        # including a lone brace quoted inside leading prose, and had no
        # way to back out once it picked that (wrong) start. The
        # raw_decode-based scan rejects a false start (JSONDecodeError)
        # and retries from the next candidate bracket, so this now
        # resolves to the real object instead of returning garbage.
        raw = 'ignore this "{" then {"ok": true}'
        self.assertEqual(extract_json_blob(raw), '{"ok": true}')

    def test_truncated_json_falls_back_to_first_bracket_slice(self) -> None:
        # No candidate bracket ever decodes successfully (output cut short
        # by max_tokens, say) — falls back to slicing from the *first*
        # bracket found (not the stripped whole string), so repair_json
        # still gets a clean truncated-object slice to work with instead of
        # having to also strip leading prose itself.
        raw = 'Here is the answer: {"a": 1, "b": {"c": 2'
        self.assertEqual(extract_json_blob(raw), '{"a": 1, "b": {"c": 2')

    def test_no_bracket_at_all_falls_back_to_stripped_input(self) -> None:
        raw = "  not json at all  "
        self.assertEqual(extract_json_blob(raw), "not json at all")

    def test_truncated_json_after_a_false_start_skips_the_false_start(self) -> None:
        # Compounding case flagged in review: a false start (a brace
        # quoted inside leading prose) followed by the real object, which
        # is itself truncated. The false start fails mid-string (its
        # JSONDecodeError.pos is well before end-of-content) — not a
        # truncation, so it must not anchor the fallback. Only the real
        # candidate's failure reaches end-of-content, so that's what the
        # fallback should slice from.
        raw = 'ignore this "{" then {"a": 1'
        self.assertEqual(extract_json_blob(raw), '{"a": 1')

    def test_no_eof_failure_at_all_falls_back_to_stripped_input(self) -> None:
        # Candidates exist but none fail from running off the end (all
        # fail mid-string, e.g. pure false starts) — there's no genuinely
        # truncated candidate to hand to repair_json, so fall back to the
        # full stripped input rather than anchoring on a wrong start.
        raw = 'ignore this "{" then not json at all'
        self.assertEqual(extract_json_blob(raw), raw.strip())

    def test_longest_match_preferred_over_incidental_empty_object(self) -> None:
        # An incidental `{}` earlier in the prose (e.g. an example config)
        # is valid JSON on its own — "first success wins" would silently
        # return it instead of the real, larger answer that follows.
        raw = 'See config {} for defaults. Actual answer: {"result": 42}'
        self.assertEqual(extract_json_blob(raw), '{"result": 42}')

    def test_longest_match_preferred_over_citation_marker(self) -> None:
        # `[1]` is a common citation-marker shape in LLM prose and is also
        # valid JSON (a single-element array) — same failure mode as above.
        raw = 'According to source [1], the result is: {"result": 42}'
        self.assertEqual(extract_json_blob(raw), '{"result": 42}')

    def test_fence_with_embedded_triple_backtick_in_string_value(self) -> None:
        # A literal ``` inside a JSON string field used to make a
        # non-greedy closing-fence regex match there instead of the real
        # fence end, truncating the JSON mid-string. There's no
        # fence-boundary regex at all anymore — backticks aren't bracket
        # characters, so they're just ignored surrounding text.
        raw = '```json\n{"note": "use ``` for code blocks", "value": 1}\n```'
        extracted = extract_json_blob(raw)
        self.assertEqual(
            json.loads(extracted),
            {"note": "use ``` for code blocks", "value": 1},
        )

    def test_unfenced_json_survives_unrelated_fence_later_in_content(self) -> None:
        # Regression test: an earlier version stripped everything before
        # the *first* ``` fence marker found anywhere in the content, on
        # the assumption that a fence always wraps the real JSON. That's
        # wrong when the real JSON is unfenced and an unrelated fenced
        # snippet (e.g. an illustrative code example) appears later — the
        # old code discarded the real JSON along with the prose before
        # the unrelated fence.
        raw = 'Answer: {"result": true}\n\nFor reference:\n```python\nx = 1\n```\n'
        self.assertEqual(extract_json_blob(raw), '{"result": true}')

    def test_pathological_bracket_run_does_not_hang(self) -> None:
        # A model stuck in a repetition loop emitting nothing but bracket
        # characters used to be O(n^2): every position was a candidate
        # whose raw_decode attempt scanned to end-of-string before failing.
        raw = "[" * 5000
        started = time.monotonic()
        extract_json_blob(raw)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_candidate_attempts_are_capped(self) -> None:
        # Structural check (not timing-based) that the cap actually bounds
        # work: with a small cap, enough failed candidates before the real
        # object exhausts the budget and the real object is never reached;
        # with the real (much larger) default cap, it is.
        raw = 'prose {x{x{x{x{"ok": true}'
        with patch.object(llm_json, "_MAX_CANDIDATE_ATTEMPTS", 4):
            capped_result = extract_json_blob(raw)
        uncapped_result = extract_json_blob(raw)
        self.assertNotEqual(capped_result, '{"ok": true}')
        self.assertEqual(uncapped_result, '{"ok": true}')

    def test_multiple_complete_objects_prefers_the_larger_one(self) -> None:
        # Locks in the documented "longest wins" policy for the case a
        # reviewer specifically flagged: a model emitting a complete
        # intermediate/CoT object before its final answer. Both are fully
        # valid, complete JSON on their own — this pins which one the
        # scanner returns so a future change can't silently flip it.
        raw = (
            'Considering: {"draft": true}\n\n'
            'Final answer: {"draft": false, "result": 42, "notes": "done"}'
        )
        self.assertEqual(
            extract_json_blob(raw),
            '{"draft": false, "result": 42, "notes": "done"}',
        )


class ParseLlmJsonTests(unittest.TestCase):
    def test_valid_json_parses_without_repair(self) -> None:
        self.assertEqual(parse_llm_json('{"ok": true}'), {"ok": True})

    def test_valid_array_parses(self) -> None:
        self.assertEqual(parse_llm_json("[1, 2, 3]"), [1, 2, 3])

    def test_unescaped_quote_triggers_repair(self) -> None:
        raw = '{"note": "she said "hi""}'
        result = parse_llm_json(raw)
        self.assertIn("note", result)

    def test_trailing_prose_with_braces_parses_successfully(self) -> None:
        raw = 'Result: {"ok": true} but note {not json}'
        self.assertEqual(parse_llm_json(raw), {"ok": True})

    def test_logger_warns_on_repair(self) -> None:
        raw = '{"note": "she said "hi""}'
        logger = logging.getLogger("test_llm_json")
        with self.assertLogs(logger, level="WARNING"):
            parse_llm_json(raw, logger=logger)

    def test_repair_returning_already_parsed_object_is_not_double_loaded(self) -> None:
        with patch("app.services.llm_json.repair_json", return_value={"ok": True}):
            result = parse_llm_json("{not valid json at all")
        self.assertEqual(result, {"ok": True})

    def test_repair_returning_string_is_loaded(self) -> None:
        with patch("app.services.llm_json.repair_json", return_value='{"ok": true}'):
            result = parse_llm_json("{not valid json at all")
        self.assertEqual(result, {"ok": True})

    def test_unrecoverable_input_raises(self) -> None:
        with (
            patch("app.services.llm_json.repair_json", return_value="still not json"),
            self.assertRaises(json.JSONDecodeError),
        ):
            parse_llm_json("{completely broken")

    def test_repair_returning_non_str_scalar_is_not_double_loaded(self) -> None:
        # Only `str` repair results get re-parsed now (not just non-dict/
        # non-list). A non-str, non-dict/list scalar (e.g. a future
        # return_objects=True path returning a bare int) used to hit
        # json.loads(42), which raises TypeError instead of
        # JSONDecodeError — a type callers wouldn't be catching.
        with patch("app.services.llm_json.repair_json", return_value=42):
            result = parse_llm_json("{not valid json at all")
        self.assertEqual(result, 42)

    def test_repair_returning_none_is_not_double_loaded(self) -> None:
        with patch("app.services.llm_json.repair_json", return_value=None):
            result = parse_llm_json("{not valid json at all")
        self.assertIsNone(result)
