"""
Build the Neo4j knowledge graph from the enriched input/ JSON files.

Nodes:
  (:Case)   — one per precedent
  (:Judge)  — one per named justice

Relationships:
  (j:Judge)-[:AUTHORED_MAJORITY]->(c:Case)
  (j:Judge)-[:JOINED_MAJORITY]->(c:Case)
  (j:Judge)-[:DISSENTED]->(c:Case)
  (j:Judge)-[:CONCURRED]->(c:Case)
  (c1:Case)-[:CITES {favorable: bool}]->(c2:Case)
  (c1:Case)-[:OVERRULES]->(c2:Case)
  (c1:Case)-[:AFFIRMS]->(c2:Case)
"""

import json
import os
import re
import glob

from neo4j import GraphDatabase

NEO4J_URI      = "bolt://localhost:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "password"

INPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "input")


# ── helpers ───────────────────────────────────────────────────────────────────

US_CITE_RE = re.compile(r"(\d{3})\s+U\.S\.\s+(\d+)")

def extract_cited_reporters(text: str) -> set[str]:
    """Return all 'NNN U.S. PPP' strings found in text."""
    return {f"{m.group(1)} U.S. {m.group(2)}" for m in US_CITE_RE.finditer(text or "")}


def detect_overrule(text: str, cited_reporter: str) -> bool:
    """Return True if text overrules the case at cited_reporter."""
    pattern = re.compile(
        r"overrul\w*[^.]{0,120}" + re.escape(cited_reporter), re.IGNORECASE
    )
    pattern2 = re.compile(
        re.escape(cited_reporter) + r"[^.]{0,120}overrul\w*", re.IGNORECASE
    )
    return bool(pattern.search(text)) or bool(pattern2.search(text))


def detect_affirm(text: str, cited_reporter: str) -> bool:
    pattern = re.compile(
        r"affirm\w*[^.]{0,120}" + re.escape(cited_reporter), re.IGNORECASE
    )
    pattern2 = re.compile(
        re.escape(cited_reporter) + r"[^.]{0,120}affirm\w*", re.IGNORECASE
    )
    return bool(pattern.search(text)) or bool(pattern2.search(text))


# ── Neo4j operations ──────────────────────────────────────────────────────────

def merge_case(tx, rec: dict):
    tx.run(
        """
        MERGE (c:Case {oyez_url: $oyez_url})
        SET c.bluebook_citation  = $bluebook_citation,
            c.case_name          = $case_name,
            c.reporter           = $reporter,
            c.subject_matter     = $subject_matter_type,
            c.venue_court        = $venue_court,
            c.winner             = $winner,
            c.key_holdings       = $key_holdings,
            c.key_arguments      = $key_arguments
        """,
        oyez_url=rec["source_url"],
        reporter=rec.get("reporter", ""),
        bluebook_citation=rec.get("bluebook_citation", ""),
        case_name=rec.get("case_name", rec.get("bluebook_citation", "")),
        subject_matter_type=rec.get("subject_matter_type", ""),
        venue_court=rec.get("venue_court", ""),
        winner=rec.get("winner", ""),
        key_holdings=(rec.get("key_holdings") or "")[:2000],
        key_arguments=(rec.get("key_arguments") or "")[:1000],
    )


def merge_judge_and_edges(tx, judge_name: str, oyez_url: str, rel_type: str):
    tx.run(
        f"""
        MERGE (j:Judge {{name: $name}})
        WITH j
        MATCH (c:Case {{oyez_url: $oyez_url}})
        MERGE (j)-[:{rel_type}]->(c)
        """,
        name=judge_name,
        oyez_url=oyez_url,
    )


def merge_case_relationship(tx, from_url: str, to_url: str, rel_type: str):
    tx.run(
        f"""
        MATCH (a:Case {{oyez_url: $from_url}})
        MATCH (b:Case {{oyez_url: $to_url}})
        MERGE (a)-[:{rel_type}]->(b)
        """,
        from_url=from_url,
        to_url=to_url,
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    with driver.session() as session:
        session.run("CREATE INDEX case_oyez_url IF NOT EXISTS FOR (c:Case) ON (c.oyez_url)")
        session.run("CREATE INDEX judge_name IF NOT EXISTS FOR (j:Judge) ON (j.name)")

    files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.json")))
    records = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))

    # Build reporter → url lookup for cross-referencing (only non-generic reporters)
    reporter_to_url: dict[str, str] = {}
    for rec in records:
        r = rec.get("reporter", "").strip()
        if r and r != "U.S.":
            reporter_to_url[r] = rec["source_url"]

    print(f"Ingesting {len(records)} cases into Neo4j …\n")

    for i, rec in enumerate(records, 1):
        oyez_url = rec["source_url"]
        reporter = rec.get("reporter", "").strip()
        bluebook = rec.get("bluebook_citation", "")
        full_text = (rec.get("full_text") or "") + " " + (rec.get("key_holdings") or "")

        with driver.session() as session:
            session.execute_write(merge_case, rec)

            judge = (rec.get("judge") or "").strip()
            if judge:
                session.execute_write(merge_judge_and_edges, judge, oyez_url, "AUTHORED_MAJORITY")

            for jname in rec.get("majority_justices") or []:
                if jname and jname != judge:
                    session.execute_write(merge_judge_and_edges, jname, oyez_url, "JOINED_MAJORITY")

            for jname in rec.get("dissenting_justices") or []:
                if jname:
                    session.execute_write(merge_judge_and_edges, jname, oyez_url, "DISSENTED")

            for jname in rec.get("concurring_justices") or []:
                if jname:
                    session.execute_write(merge_judge_and_edges, jname, oyez_url, "CONCURRED")

            cited = extract_cited_reporters(full_text)
            for cited_rep in cited:
                if cited_rep == reporter or cited_rep not in reporter_to_url:
                    continue
                to_url = reporter_to_url[cited_rep]
                if detect_overrule(full_text, cited_rep):
                    rel = "OVERRULES"
                elif detect_affirm(full_text, cited_rep):
                    rel = "AFFIRMS"
                else:
                    rel = "CITES"
                session.execute_write(merge_case_relationship, oyez_url, to_url, rel)

        print(f"[{i:02d}/{len(records)}] {bluebook[:65]}")

    driver.close()

    print("\nGraph build complete.")
    print("Node/edge summary query:")
    print("  MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC")


if __name__ == "__main__":
    main()
