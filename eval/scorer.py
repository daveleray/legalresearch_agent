"""
Scoring logic for benchmark evaluation.

Metrics per query
-----------------
citation_recall     — fraction of expected citations found in results
citation_precision  — fraction of returned results that are expected
citation_f1         — harmonic mean of recall and precision
holding_coverage    — fraction of key holding phrases found in returned holdings text
composite           — equal-weight average of f1 and holding_coverage
"""

from __future__ import annotations
import re


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _reporter_in_text(reporter_fragment: str, text: str) -> bool:
    """Check if a reporter like '509 U.S. 209' appears in text."""
    return _normalise(reporter_fragment) in _normalise(text)


def _name_in_text(case_name: str, text: str) -> bool:
    """Fuzzy: check if the first meaningful word of the case name appears."""
    first_word = re.split(r"[\s,.]", case_name)[0].lower()
    if len(first_word) < 4:
        # use first two words for short names
        words = case_name.lower().split()
        first_word = " ".join(words[:2])
    return first_word in _normalise(text)


def score_memo(gold: dict, memo_text: str) -> dict:
    """
    Score free-form memo text (e.g. the output of the legal research agent)
    against a golden entry. Delegates to score_results with the text wrapped
    as a single result dict so all citation/phrase logic is shared.
    """
    return score_results(gold, [{"key_holdings": memo_text}])


def score_results(gold: dict, results: list[dict]) -> dict:
    """
    gold    — one entry from golden.json
    results — list of dicts returned by a retriever
    """
    # Combine all returned text for phrase matching (include full_text_snippet for
    # cases whose key_holdings are empty but whose LOC PDF text is populated)
    all_returned_text = " ".join(
        " ".join(filter(None, [
            r.get("key_holdings") or "",
            r.get("full_text_snippet") or "",
            r.get("case_name") or "",
            r.get("bluebook_citation") or "",
            r.get("reporter") or "",
        ]))
        for r in results
    )

    # ── Citation recall ───────────────────────────────────────────────────────
    expected_reporters = gold.get("expected_reporter_fragments", [])
    expected_names     = gold.get("expected_case_names", [])

    # Score per expected case: try reporter fragment first, fall back to name.
    # This handles cases where the formal US Reports citation isn't in our data
    # yet (recent decisions without a published volume/page) — the case may still
    # be correctly retrieved, just without a citable reporter string.
    expected_total = len(expected_names) if expected_names else len(expected_reporters)
    hits_recall = 0
    for i, name in enumerate(expected_names):
        frag = expected_reporters[i] if i < len(expected_reporters) else None
        if frag and _reporter_in_text(frag, all_returned_text):
            hits_recall += 1          # reporter match — definitive
        elif _name_in_text(name, all_returned_text):
            hits_recall += 1          # name fallback — case present but uncited

    citation_recall = hits_recall / expected_total if expected_total else 0.0

    # ── Citation precision ────────────────────────────────────────────────────
    returned_total = len(results)
    hits_precision = 0
    for r in results:
        r_text = (r.get("bluebook_citation") or "") + " " + (r.get("reporter") or "") + " " + (r.get("case_name") or "")
        matched = any(_reporter_in_text(frag, r_text) for frag in expected_reporters)
        if not matched:
            matched = any(_name_in_text(name, r_text) for name in expected_names)
        if matched:
            hits_precision += 1

    citation_precision = hits_precision / returned_total if returned_total else 0.0

    # ── Citation F1 ───────────────────────────────────────────────────────────
    if citation_recall + citation_precision > 0:
        citation_f1 = 2 * citation_recall * citation_precision / (citation_recall + citation_precision)
    else:
        citation_f1 = 0.0

    # ── Holding coverage ──────────────────────────────────────────────────────
    key_phrases = gold.get("key_holding_phrases", [])
    phrase_hits = sum(
        1 for p in key_phrases if _normalise(p) in _normalise(all_returned_text)
    )
    holding_coverage = phrase_hits / len(key_phrases) if key_phrases else 0.0

    # ── Composite ─────────────────────────────────────────────────────────────
    composite = (citation_f1 + holding_coverage) / 2

    return {
        "citation_recall":    round(citation_recall, 3),
        "citation_precision": round(citation_precision, 3),
        "citation_f1":        round(citation_f1, 3),
        "holding_coverage":   round(holding_coverage, 3),
        "composite":          round(composite, 3),
        "expected_total":     expected_total,
        "returned_total":     returned_total,
        "hits_recall":        hits_recall,
        "hits_precision":     hits_precision,
        "phrase_hits":        phrase_hits,
        "phrase_total":       len(key_phrases),
    }
