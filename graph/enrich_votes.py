"""
Re-fetch Oyez vote data for every case in input/ and write an enriched
votes.json sidecar next to each case file.

Adds:
  majority_justices  — list of justice names who voted with the majority
  dissenting_justices — list of justice names who dissented
  concurring_justices — list of justice names who concurred only
"""
import json, time, re, os, glob, requests
from bs4 import BeautifulSoup

INPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "input")
DELAY = 0.8
HEADERS = {"Accept": "application/json", "User-Agent": "legal-research-bot/0.1"}


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_votes(detail):
    decisions = detail.get("decisions") or []
    if not decisions:
        return [], [], []

    majority, dissenting, concurring = [], [], []
    for decision in decisions:
        for vote in decision.get("votes") or []:
            name = (vote.get("member") or {}).get("name", "")
            if not name:
                continue
            vote_type = (vote.get("vote") or "").lower()
            if vote_type == "majority":
                majority.append(name)
            elif vote_type == "minority":
                dissenting.append(name)
            elif "concurr" in vote_type:
                concurring.append(name)
        break  # use first (main) decision only

    return list(dict.fromkeys(majority)), list(dict.fromkeys(dissenting)), list(dict.fromkeys(concurring))


def main():
    files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.json")))
    # skip existing votes files
    files = [f for f in files if "_votes" not in f]
    print(f"Enriching {len(files)} cases with vote data …\n")

    for i, path in enumerate(files, 1):
        fname = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)

            source_url = rec.get("source_url", "")
            if not source_url:
                print(f"[{i:02d}] SKIP {fname} — no source_url")
                continue

            detail = get_json(source_url)
            if isinstance(detail, list):
                print(f"[{i:02d}] SKIP {fname} — Oyez returned list")
                continue

            maj, dis, con = parse_votes(detail)
            rec["majority_justices"] = maj
            rec["dissenting_justices"] = dis
            rec["concurring_justices"] = con

            with open(path, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, ensure_ascii=False)

            print(f"[{i:02d}] OK  {rec['bluebook_citation'][:60]}  maj={len(maj)} dis={len(dis)}")
        except Exception as e:
            print(f"[{i:02d}] ERR {fname} — {e}")
        time.sleep(DELAY)

    print("\nDone.")


if __name__ == "__main__":
    main()
