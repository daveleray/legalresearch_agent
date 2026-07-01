"""
Live search-and-ingest pipeline.

Given a user query:
  1. Check local coverage (run retriever, score against rough keyword match).
  2. If coverage is weak (< COVERAGE_THRESHOLD), search Oyez:
       - Fetch N recent terms, filter case stubs by keyword overlap on name/description.
       - For top candidates not already in DB, fetch full Oyez detail + LOC PDF.
       - Ingest into SQLite and Neo4j.
  3. Re-run retriever and return enriched results.
  4. Log the query and result to the query layer.

Over time the local DB grows, raising coverage scores and reducing Oyez round-trips.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import glob
import uuid

import requests
from bs4 import BeautifulSoup
from neo4j import GraphDatabase

from pipeline.full_text import fetch_full_text

DB_PATH    = os.path.join(os.path.dirname(__file__), "..", "db", "precedents.db")
INPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "input")
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "password")
OYEZ_BASE  = "https://api.oyez.org"

COVERAGE_THRESHOLD = 0.50   # composite score below this triggers Oyez search
OYEZ_TERMS         = list(range(2023, 2009, -1))   # newest first
MAX_NEW_CASES      = 10     # cap ingestion per query
DELAY              = 0.8

HEADERS = {"Accept": "application/json", "User-Agent": "legal-research-bot/0.1"}


# ── keyword helpers ───────────────────────────────────────────────────────────

STOPWORDS = {
    # question words
    "what","is","the","a","an","of","in","for","and","or","to","give","me",
    "short","memo","how","when","does","can","must","under","are","prove",
    "establish","applies","leading","authorities","standard","current","test",
    # generic legal/court terms that appear in almost any case
    "court","held","case","rule","law","act","section","federal","united",
    "states","supreme","decision","opinion","holding","claim","plaintiff",
    "defendant","petitioner","respondent","review","appeal","circuit",
    "district","rights","right","government","state","constitutional",
    "statute","statutory","whether","pursuant","violated","violation",
    # antitrust generic
    "sherman","antitrust","competition","competitive",
}

def keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z]{3,}", text.lower())
    return [t for t in tokens if t not in STOPWORDS]


def keyword_score(tokens: list[str], text: str) -> int:
    t = text.lower()
    return sum(1 for tok in tokens if tok in t)


# ── local coverage check ──────────────────────────────────────────────────────

def _local_coverage(query: str, subject_matter: str) -> float:
    """
    Coverage score based on keyword overlap in key_holdings only —
    the focused per-case summary of what the case actually decided.
    full_text is excluded so that incidental mentions in 80k-char
    opinions don't falsely inflate coverage.
    Normalised: 0.0 (no on-topic cases) to 1.0 (3+ matching cases).
    """
    kws = keywords(query)
    if not kws:
        return 0.0
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    sm_clause = "AND subject_matter_type = ?" if subject_matter else ""
    or_cond   = " OR ".join("LOWER(key_holdings) LIKE ?" for _ in kws)
    expanded  = [f"%{kw}%" for kw in kws]
    if subject_matter:
        expanded.append(subject_matter)
    cur.execute(
        f"SELECT COUNT(*) FROM precedents WHERE ({or_cond}) {sm_clause}",
        expanded,
    )
    matching = cur.fetchone()[0]
    conn.close()
    return min(matching / 3.0, 1.0)


# ── Oyez multi-term search ────────────────────────────────────────────────────

def _existing_oyez_urls() -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    # We store oyez URL in the source_url column via the JSON files;
    # check input/ files instead since SQLite doesn't store oyez_url directly.
    conn.close()
    urls = set()
    for path in glob.glob(os.path.join(INPUT_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                urls.add(json.load(f).get("source_url", ""))
        except Exception:
            pass
    return urls


def _search_oyez(query: str, max_results: int = 20) -> list[dict]:
    """
    Fetch recent Oyez term listings, score each case by keyword overlap
    on name + description + question, return top candidates not already in DB.
    """
    kws     = keywords(query)
    seen    = _existing_oyez_urls()
    scored: list[tuple[int, dict]] = []

    for term in OYEZ_TERMS:
        try:
            r = requests.get(
                f"{OYEZ_BASE}/cases?per_page=100&filter=term:{term}",
                headers=HEADERS, timeout=15,
            )
            stubs = r.json() if r.ok else []
        except Exception:
            continue

        for stub in stubs:
            url = stub.get("href", "")
            if url in seen:
                continue
            text = " ".join(filter(None, [
                stub.get("name", ""),
                stub.get("description", "") or "",
                stub.get("question", "") or "",
            ]))
            sc = keyword_score(kws, text)
            if sc >= 2:   # require at least 2 distinct keywords to match
                stub["_score"] = sc
                scored.append((sc, stub))

        time.sleep(0.3)

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:max_results]]


# ── Oyez case fetcher (reuses scraper logic) ──────────────────────────────────

def _strip_html(text):
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True)


def _fetch_oyez_case(stub: dict) -> dict | None:
    try:
        r = requests.get(stub["href"], headers=HEADERS, timeout=20)
        detail = r.json()
        if not isinstance(detail, dict):
            return None
    except Exception:
        return None

    citation = detail.get("citation") or {}
    vol  = citation.get("volume", "")
    page = citation.get("page", "")
    year = citation.get("year", "")
    name = detail.get("name", "Unknown")

    reporter          = f"{vol} U.S. {page}" if vol and page else "U.S."
    bluebook_citation = f"{name}, {vol} U.S. {page} ({year})" if vol and page else name

    # Votes
    decisions = detail.get("decisions") or []
    majority, dissenting, concurring = [], [], []
    for dec in decisions[:1]:
        for v in (dec.get("votes") or []):
            jname     = (v.get("member") or {}).get("name", "")
            vote_type = (v.get("vote") or "").lower()
            if vote_type == "majority":    majority.append(jname)
            elif vote_type == "minority":  dissenting.append(jname)
            elif "concurr" in vote_type:   concurring.append(jname)

    # Opinion author
    judge = ""
    for op in (detail.get("written_opinion") or []):
        if op.get("type", {}).get("value") == "majority":
            judge = op.get("judge_full_name") or op.get("judge_last_name") or ""
            break
    if not judge:
        m = re.search(
            r"(Chief Justice|Justice)\s+([A-Z][a-z]+(?: [A-Z][a-z.]+)*)\s+authored",
            _strip_html(detail.get("conclusion", "")),
        )
        if m:
            judge = m.group(0)

    # Winner heuristic
    conclusion = _strip_html(detail.get("conclusion", "")).lower()
    if "reversed" in conclusion or "vacated" in conclusion:
        winner = "petitioner"
    elif "affirmed" in conclusion:
        winner = "respondent"
    else:
        winner = "unknown"

    # Texts
    facts    = _strip_html(detail.get("facts_of_the_case", ""))
    question = _strip_html(detail.get("question", ""))
    concl    = _strip_html(detail.get("conclusion", ""))
    oyez_summary = "\n\n".join(filter(None, [facts, question, concl]))

    # Attempt LOC full text
    full_text, text_source = fetch_full_text(reporter, oyez_summary)

    return {
        "bluebook_citation":   bluebook_citation,
        "reporter":            reporter,
        "subject_matter_type": "",
        "key_holdings":        concl[:5000],
        "venue_court":         "Supreme Court of the United States",
        "judge":               judge,
        "winner":              winner,
        "full_text":           full_text,
        "key_arguments":       " | ".join(filter(None, [facts[:2000], question[:500]])),
        "source_url":          stub["href"],
        "case_name":           name,
        "majority_justices":   majority,
        "dissenting_justices": dissenting,
        "concurring_justices": concurring,
        "_text_source":        text_source,
    }


# ── SQLite ingest ─────────────────────────────────────────────────────────────

SQLITE_FIELDS = [
    "bluebook_citation","reporter","subject_matter_type",
    "key_holdings","venue_court","judge","winner","full_text","key_arguments",
]

def _ingest_sqlite(rec: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    values = [rec.get(f, "") or "" for f in SQLITE_FIELDS]
    cur.execute(
        f"INSERT OR IGNORE INTO precedents ({','.join(SQLITE_FIELDS)}) "
        f"VALUES ({','.join(['?']*len(SQLITE_FIELDS))})",
        values,
    )
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


# ── Neo4j ingest ──────────────────────────────────────────────────────────────

def _ingest_neo4j(rec: dict):
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    def merge_case(tx, r):
        tx.run(
            """
            MERGE (c:Case {oyez_url: $url})
            SET c.bluebook_citation = $bc,
                c.case_name         = $name,
                c.reporter          = $rep,
                c.subject_matter    = $sm,
                c.venue_court       = $vc,
                c.winner            = $win,
                c.key_holdings      = $kh,
                c.key_arguments     = $ka
            """,
            url=r["source_url"], bc=r["bluebook_citation"],
            name=r["case_name"], rep=r["reporter"],
            sm=r["subject_matter_type"], vc=r["venue_court"],
            win=r["winner"],
            kh=(r["key_holdings"] or "")[:2000],
            ka=(r["key_arguments"] or "")[:1000],
        )

    def merge_judge(tx, jname, url, rel):
        tx.run(
            f"""
            MERGE (j:Judge {{name: $name}})
            WITH j MATCH (c:Case {{oyez_url: $url}})
            MERGE (j)-[:{rel}]->(c)
            """,
            name=jname, url=url,
        )

    with driver.session() as s:
        s.execute_write(merge_case, rec)
        url = rec["source_url"]
        if rec.get("judge"):
            s.execute_write(merge_judge, rec["judge"], url, "AUTHORED_MAJORITY")
        for j in rec.get("majority_justices") or []:
            if j and j != rec.get("judge"):
                s.execute_write(merge_judge, j, url, "JOINED_MAJORITY")
        for j in rec.get("dissenting_justices") or []:
            if j:
                s.execute_write(merge_judge, j, url, "DISSENTED")
        for j in rec.get("concurring_justices") or []:
            if j:
                s.execute_write(merge_judge, j, url, "CONCURRED")

    driver.close()


# ── save to input/ ────────────────────────────────────────────────────────────

def _save_json(rec: dict):
    existing = glob.glob(os.path.join(INPUT_DIR, "*.json"))
    idx = len(existing) + 1
    slug = re.sub(r"[^a-z0-9]+", "_", rec["case_name"].lower())[:50]
    path = os.path.join(INPUT_DIR, f"{idx:02d}_{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)


# ── public entry point ────────────────────────────────────────────────────────

def search_and_ingest(
    query: str,
    subject_matter: str = "",
    force: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Check local coverage; if weak, search Oyez, ingest new cases, report.

    Returns:
    {
      "coverage_before": float,
      "coverage_after":  float,
      "searched_oyez":   bool,
      "candidates_found": int,
      "new_cases_added": list[str],   # bluebook citations
      "skipped":         int,
    }
    """
    coverage_before = _local_coverage(query, subject_matter)
    result = {
        "coverage_before":  round(coverage_before, 3),
        "coverage_after":   round(coverage_before, 3),
        "searched_oyez":    False,
        "candidates_found": 0,
        "new_cases_added":  [],
        "skipped":          0,
    }

    if coverage_before >= COVERAGE_THRESHOLD and not force:
        if verbose:
            print(f"Local coverage {coverage_before:.2f} >= {COVERAGE_THRESHOLD} -- no Oyez search needed.")
        return result

    if verbose:
        print(f"Local coverage {coverage_before:.2f} < {COVERAGE_THRESHOLD} -- searching Oyez ...")

    candidates = _search_oyez(query, max_results=MAX_NEW_CASES * 2)
    result["searched_oyez"]    = True
    result["candidates_found"] = len(candidates)

    if verbose:
        print(f"  Found {len(candidates)} Oyez candidates.")

    added, skipped = [], 0
    for stub in candidates[:MAX_NEW_CASES]:
        rec = _fetch_oyez_case(stub)
        if rec is None:
            skipped += 1
            continue

        sqlite_new = _ingest_sqlite(rec)
        if sqlite_new:
            _ingest_neo4j(rec)
            _save_json(rec)
            added.append(rec["bluebook_citation"])
            if verbose:
                src = rec.get("_text_source", "?")
                print(f"  + ingested [{src}] {rec['bluebook_citation'][:65]}")
        else:
            skipped += 1

        time.sleep(DELAY)

    result["new_cases_added"] = added
    result["skipped"]         = skipped
    result["coverage_after"]  = round(_local_coverage(query, subject_matter), 3)

    if verbose:
        print(f"\n  Added {len(added)} cases. Coverage: {coverage_before:.2f} -> {result['coverage_after']:.2f}")

    return result


if __name__ == "__main__":
    print("=== Test: predatory pricing query ===\n")
    r = search_and_ingest(
        "What are the leading authorities for predatory pricing?",
        subject_matter="antitrust",
        verbose=True,
    )
    print("\nResult:", json.dumps({k: v for k, v in r.items() if k != "new_cases_added"}, indent=2))
    if r["new_cases_added"]:
        print("New cases:")
        for c in r["new_cases_added"]:
            print(f"  {c}")
