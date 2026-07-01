"""
Download ~30 SCOTUS cases from the Oyez public API and save each as JSON to ../input/.
"""
import json
import time
import re
import os
import requests
from bs4 import BeautifulSoup

OYEZ_BASE = "https://api.oyez.org"
INPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "input")
TARGET = 30
DELAY = 1.0
TERMS = ["2023", "2022", "2021"]  # walk newest first until we have enough

HEADERS = {"Accept": "application/json", "User-Agent": "legal-research-bot/0.1"}


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def strip_html(text):
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True)


def list_term_cases(term, limit):
    data = get_json(f"{OYEZ_BASE}/cases?per_page={limit}&filter=term:{term}")
    return data if isinstance(data, list) else []


def extract_majority_author(detail):
    """Pull the majority opinion author from written_opinion list."""
    for op in detail.get("written_opinion") or []:
        if op.get("type", {}).get("value") == "majority":
            return op.get("judge_full_name") or op.get("judge_last_name") or ""
    # Fall back: parse conclusion text
    conclusion = strip_html(detail.get("conclusion", ""))
    m = re.search(r"(Chief Justice|Justice)\s+([A-Z][a-z]+(?: [A-Z][a-z.]+)*)\s+authored", conclusion)
    return m.group(0) if m else ""


def infer_winner(detail, first_party, second_party):
    """
    Heuristic: check decisions[0].description for reversal language;
    also try to match first/second party labels.
    """
    decisions = detail.get("decisions") or []
    conclusion = strip_html(detail.get("conclusion", "")).lower()
    desc = strip_html(decisions[0].get("description", "")).lower() if decisions else ""
    text = desc + " " + conclusion

    if "reversed" in text or "vacated" in text or "remanded" in text:
        return "petitioner"
    if "affirmed" in text:
        return "respondent"

    first = (first_party or "").lower()
    second = (second_party or "").lower()
    for party in [first, second]:
        if party and party in text:
            return party

    return "unknown"


def parse_case(stub):
    detail = get_json(stub["href"])

    citation = detail.get("citation") or {}
    vol = citation.get("volume", "")
    page = citation.get("page", "")
    year = citation.get("year", "")
    name = detail.get("name", "Unknown")

    bluebook_citation = (
        f"{name}, {vol} U.S. {page} ({year})" if vol and page else name
    )
    reporter = f"{vol} U.S. {page}" if vol and page else "U.S."

    first_party = detail.get("first_party") or ""
    second_party = detail.get("second_party") or ""

    # Key holdings: conclusion field (HTML → plain text)
    key_holdings = strip_html(detail.get("conclusion", ""))[:5000]

    # Key arguments: facts_of_the_case + question
    facts = strip_html(detail.get("facts_of_the_case", ""))
    question = strip_html(detail.get("question", ""))
    key_arguments = " | ".join(filter(None, [facts[:2000], question[:500]]))

    # Full text = everything narrative we have
    full_text = "\n\n".join(filter(None, [
        strip_html(detail.get("facts_of_the_case", "")),
        strip_html(detail.get("question", "")),
        strip_html(detail.get("conclusion", "")),
    ]))[:20000]

    judge = extract_majority_author(detail)
    venue_court = "Supreme Court of the United States"
    winner = infer_winner(detail, first_party, second_party)

    return {
        "bluebook_citation": bluebook_citation,
        "reporter": reporter,
        "subject_matter_type": "",   # to be enriched later
        "key_holdings": key_holdings,
        "venue_court": venue_court,
        "judge": judge,
        "winner": winner,
        "full_text": full_text,
        "key_arguments": key_arguments,
        "source_url": stub.get("href", ""),
        "case_name": name,
    }


def main():
    os.makedirs(INPUT_DIR, exist_ok=True)
    stubs = []
    for term in TERMS:
        needed = TARGET - len(stubs)
        if needed <= 0:
            break
        print(f"Fetching term {term} case list …")
        batch = list_term_cases(term, needed)
        stubs.extend(batch)
        time.sleep(DELAY)

    stubs = stubs[:TARGET]
    print(f"Scraping {len(stubs)} cases …\n")

    success, errors = 0, 0
    for i, stub in enumerate(stubs, 1):
        try:
            case = parse_case(stub)
            slug = re.sub(r"[^a-z0-9]+", "_", case["case_name"].lower())[:50]
            fname = f"{i:02d}_{slug}.json"
            out_path = os.path.join(INPUT_DIR, fname)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(case, f, indent=2, ensure_ascii=False)
            print(f"[{i:02d}/{len(stubs)}] OK  {case['bluebook_citation'][:70]}")
            success += 1
        except Exception as e:
            print(f"[{i:02d}/{len(stubs)}] ERR {stub.get('href', '?')} — {e}")
            errors += 1
        time.sleep(DELAY)

    print(f"\nDone. {success} saved to {INPUT_DIR}, {errors} errors.")


if __name__ == "__main__":
    main()
