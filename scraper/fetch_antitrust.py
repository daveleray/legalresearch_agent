"""
Fetch the top 20 landmark SCOTUS antitrust decisions from the Oyez API.
Cases are identified by known Oyez path; fetched individually and saved to ../input/.
"""
import json
import time
import re
import os
import requests
from bs4 import BeautifulSoup

OYEZ_BASE = "https://api.oyez.org"
INPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "input")
DELAY = 1.0

HEADERS = {"Accept": "application/json", "User-Agent": "legal-research-bot/0.1"}

# Top 20 landmark SCOTUS antitrust decisions — Oyez case paths
ANTITRUST_CASES = [
    # Modern era (2000s–2020s)
    ("2020", "20-512"),    # NCAA v. Alston (2021)
    ("2020", "19-508"),    # AMG Capital Management v. FTC (2021)
    ("2018", "17-204"),    # Apple Inc. v. Pepper (2019)
    ("2017", "16-1454"),   # Ohio v. American Express Co. (2018)
    ("2014", "13-534"),    # N.C. State Bd. of Dental Examiners v. FTC (2015)
    ("2012", "12-416"),    # FTC v. Actavis, Inc. (2013)
    ("2012", "11-1160"),   # FTC v. Phoebe Putney Health System (2013)
    ("2009", "08-661"),    # American Needle, Inc. v. NFL (2010)
    ("2008", "07-512"),    # Pacific Bell Telephone v. linkLine Communications (2009)
    ("2006", "05-1126"),   # Bell Atlantic Corp. v. Twombly (2007)
    ("2006", "06-480"),    # Leegin Creative Leather Products v. PSKS (2007)
    ("2006", "05-1157"),   # Credit Suisse Securities v. Billing (2007)
    ("2006", "05-381"),    # Weyerhaeuser Co. v. Ross-Simmons Hardwood (2007)
    ("2005", "04-1329"),   # Illinois Tool Works v. Independent Ink (2006)
    ("2005", "04-805"),    # Texaco Inc. v. Dagher (2006)
    ("2003", "02-682"),    # Verizon Communications v. Law Offices of Trinko (2004)
    # Classic era (1990s)
    ("1992", "92-466"),    # Brooke Group Ltd. v. Brown & Williamson Tobacco (1993)
    ("1991", "90-1029"),   # Eastman Kodak Co. v. Image Technical Services (1992)
    ("1992", "91-10"),     # Spectrum Sports, Inc. v. McQuillan (1993)
    ("1984", "83-1812"),   # Aspen Skiing Co. v. Aspen Highlands Skiing (1985)
]


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def strip_html(text):
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True)


def extract_majority_author(detail):
    for op in detail.get("written_opinion") or []:
        if op.get("type", {}).get("value") == "majority":
            return op.get("judge_full_name") or op.get("judge_last_name") or ""
    conclusion = strip_html(detail.get("conclusion", ""))
    m = re.search(
        r"(Chief Justice|Justice)\s+([A-Z][a-z]+(?: [A-Z][a-z.]+)*)\s+authored",
        conclusion,
    )
    return m.group(0) if m else ""


def infer_winner(detail, first_party, second_party):
    decisions = detail.get("decisions") or []
    conclusion = strip_html(detail.get("conclusion", "")).lower()
    desc = strip_html(decisions[0].get("description", "")).lower() if decisions else ""
    text = desc + " " + conclusion
    if "reversed" in text or "vacated" in text or "remanded" in text:
        return "petitioner"
    if "affirmed" in text:
        return "respondent"
    return "unknown"


def parse_case(term, docket):
    url = f"{OYEZ_BASE}/cases/{term}/{docket}"
    detail = get_json(url)

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

    key_holdings = strip_html(detail.get("conclusion", ""))[:5000]

    facts = strip_html(detail.get("facts_of_the_case", ""))
    question = strip_html(detail.get("question", ""))
    key_arguments = " | ".join(filter(None, [facts[:2000], question[:500]]))

    full_text = "\n\n".join(filter(None, [
        strip_html(detail.get("facts_of_the_case", "")),
        strip_html(detail.get("question", "")),
        strip_html(detail.get("conclusion", "")),
    ]))[:20000]

    return {
        "bluebook_citation": bluebook_citation,
        "reporter": reporter,
        "subject_matter_type": "antitrust",
        "key_holdings": key_holdings,
        "venue_court": "Supreme Court of the United States",
        "judge": extract_majority_author(detail),
        "winner": infer_winner(detail, first_party, second_party),
        "full_text": full_text,
        "key_arguments": key_arguments,
        "source_url": url,
        "case_name": name,
    }


def main():
    os.makedirs(INPUT_DIR, exist_ok=True)
    # Find highest existing file index to avoid overwriting
    existing = [f for f in os.listdir(INPUT_DIR) if f.endswith(".json")]
    next_idx = len(existing) + 1

    print(f"Fetching {len(ANTITRUST_CASES)} antitrust SCOTUS cases …\n")
    success, errors = 0, 0

    for i, (term, docket) in enumerate(ANTITRUST_CASES, 1):
        try:
            case = parse_case(term, docket)
            slug = re.sub(r"[^a-z0-9]+", "_", case["case_name"].lower())[:50]
            fname = f"{next_idx:02d}_{slug}.json"
            out_path = os.path.join(INPUT_DIR, fname)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(case, f, indent=2, ensure_ascii=False)
            print(f"[{i:02d}/20] OK  {case['bluebook_citation'][:70]}")
            success += 1
            next_idx += 1
        except Exception as e:
            print(f"[{i:02d}/20] ERR {term}/{docket} — {e}")
            errors += 1
        time.sleep(DELAY)

    print(f"\nDone. {success} saved, {errors} errors.")


if __name__ == "__main__":
    main()
