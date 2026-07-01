"""
Retrieval approaches for the benchmark.

Each approach receives a query string and returns a list of dicts:
  [{"case_name": str, "reporter": str, "key_holdings": str, "oyez_url": str}, ...]

Approaches
----------
1. sqlite_keyword  — LIKE search across key_holdings + key_arguments + full_text in SQLite
2. neo4j_subject   — Neo4j: filter by subject_matter, score by holding text overlap
3. neo4j_fulltext  — Neo4j: fulltext index on key_holdings (added here if absent)
4. combined        — Union of all three, deduplicated and re-ranked; neo4j-only hits
                     enriched with SQLite full_text_snippet
"""

import os, re, sqlite3
from neo4j import GraphDatabase

DB_PATH   = os.path.join(os.path.dirname(__file__), "..", "db", "precedents.db")
NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "password")


# ── helpers ───────────────────────────────────────────────────────────────────

def _keywords(query: str) -> list[str]:
    """Extract meaningful tokens from a query string."""
    stopwords = {
        "what", "is", "the", "a", "an", "of", "in", "for", "and", "or",
        "to", "give", "me", "short", "memo", "how", "when", "does", "can",
        "must", "under", "are", "prove", "establish", "applies",
    }
    tokens = re.findall(r"[a-zA-Z]{3,}", query.lower())
    return [t for t in tokens if t not in stopwords]


def _row_to_result(row) -> dict:
    return {
        "case_name":    row[0] or "",
        "reporter":     row[1] or "",
        "key_holdings": row[2] or "",
        "bluebook_citation": row[3] or "",
        "subject_matter_type": row[4] or "",
    }


# ── Approach 1: SQLite keyword LIKE ──────────────────────────────────────────

def sqlite_keyword(query: str, subject_matter: str = "", limit: int = 5) -> list[dict]:
    keywords = _keywords(query)
    if not keywords:
        return []

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # OR across keywords so any single match qualifies; score by match count
    or_conditions = " OR ".join(
        "(LOWER(key_holdings) LIKE ? OR LOWER(key_arguments) LIKE ? OR LOWER(full_text) LIKE ?)"
        for _ in keywords
    )
    score_expr = " + ".join(
        f"(CASE WHEN LOWER(key_holdings) LIKE ? THEN 3 ELSE 0 END"
        f" + CASE WHEN LOWER(key_arguments) LIKE ? THEN 2 ELSE 0 END"
        f" + CASE WHEN LOWER(full_text) LIKE ? THEN 2 ELSE 0 END)"
        for _ in keywords
    )

    sm_clause = " AND subject_matter_type = ?" if subject_matter else ""

    sql = f"""
        SELECT bluebook_citation,
               reporter,
               key_holdings,
               subject_matter_type,
               SUBSTR(full_text, 1, 3000) AS full_text_snippet,
               ({score_expr}) AS score
        FROM precedents
        WHERE ({or_conditions}){sm_clause}
        ORDER BY score DESC
        LIMIT {limit}
    """

    where_params = [p for kw in keywords for p in [f"%{kw}%"] * 3]
    score_params = [p for kw in keywords for p in [f"%{kw}%"] * 3]
    extra = [subject_matter] if subject_matter else []

    cur.execute(sql, score_params + where_params + extra)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "case_name":           r[0],
            "reporter":            r[1],
            "key_holdings":        r[2],
            "bluebook_citation":   r[0],
            "subject_matter_type": r[3],
            "full_text_snippet":   r[4] or "",
        }
        for r in rows
    ]


# ── Approach 2: Neo4j subject-matter + holding keyword overlap ────────────────

def neo4j_subject(query: str, subject_matter: str = "", limit: int = 5) -> list[dict]:
    keywords = _keywords(query)
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    with driver.session() as s:
        if subject_matter:
            rows = s.run(
                """
                MATCH (c:Case {subject_matter: $sm})
                RETURN c.case_name         AS case_name,
                       c.reporter          AS reporter,
                       c.key_holdings      AS key_holdings,
                       c.bluebook_citation AS bluebook_citation,
                       c.subject_matter    AS subject_matter_type
                """,
                sm=subject_matter,
            ).data()
        else:
            rows = s.run(
                """
                MATCH (c:Case)
                RETURN c.case_name         AS case_name,
                       c.reporter          AS reporter,
                       c.key_holdings      AS key_holdings,
                       c.bluebook_citation AS bluebook_citation,
                       c.subject_matter    AS subject_matter_type
                """
            ).data()

    driver.close()

    def score(row):
        text = ((row.get("key_holdings") or "") + " " + (row.get("case_name") or "")).lower()
        return sum(1 for kw in keywords if kw in text)

    ranked = sorted(rows, key=score, reverse=True)
    return ranked[:limit]


# ── Approach 3: Neo4j fulltext index ─────────────────────────────────────────

def _ensure_fulltext_index(driver):
    with driver.session() as s:
        s.run(
            """
            CREATE FULLTEXT INDEX case_holdings_ft IF NOT EXISTS
            FOR (c:Case) ON EACH [c.key_holdings, c.key_arguments, c.case_name]
            """
        )


def neo4j_fulltext(query: str, subject_matter: str = "", limit: int = 5) -> list[dict]:
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    _ensure_fulltext_index(driver)

    # Build a Lucene query from keywords
    keywords = _keywords(query)
    lucene_q = " OR ".join(keywords) if keywords else "*"

    with driver.session() as s:
        rows = s.run(
            """
            CALL db.index.fulltext.queryNodes('case_holdings_ft', $q)
            YIELD node, score
            WHERE $sm = '' OR node.subject_matter = $sm
            RETURN node.case_name         AS case_name,
                   node.reporter          AS reporter,
                   node.key_holdings      AS key_holdings,
                   node.bluebook_citation AS bluebook_citation,
                   node.subject_matter    AS subject_matter_type,
                   score
            ORDER BY score DESC
            LIMIT $limit
            """,
            q=lucene_q, sm=(subject_matter or ""), limit=limit,
        ).data()

    driver.close()
    return rows


# ── Approach 4: Combined (union + re-rank + snippet enrichment) ───────────────

def _result_key(r: dict) -> str:
    """Stable dedup key: reporter string when formal, first case-name word otherwise."""
    rep = (r.get("reporter") or "").strip()
    if rep and rep not in ("U.S.", ""):
        return rep.lower()
    name = r.get("case_name") or r.get("bluebook_citation") or ""
    words = re.findall(r"[a-zA-Z]{3,}", name.lower())
    return words[0] if words else ""


def combined(query: str, subject_matter: str = "", limit: int = 5) -> list[dict]:
    """
    Union of sqlite_keyword, neo4j_subject, and neo4j_fulltext.
    Cases appearing in multiple retrievers receive an additive score boost.
    Neo4j-only hits are enriched with a SQLite full_text_snippet when available.
    """
    sq = sqlite_keyword(query, subject_matter=subject_matter, limit=10)
    ns = neo4j_subject(query, subject_matter=subject_matter, limit=10)
    nf = neo4j_fulltext(query, subject_matter=subject_matter, limit=10)

    merged: dict[str, dict] = {}
    score_map: dict[str, float] = {}

    def _incorporate(results: list[dict], weight: float, rank_penalty: float = 0.1):
        for i, r in enumerate(results):
            k = _result_key(r)
            if not k:
                continue
            bonus = weight - i * rank_penalty
            if k not in merged:
                merged[k] = dict(r)
                score_map[k] = bonus
            else:
                score_map[k] += bonus
                if len(r.get("key_holdings") or "") > len(merged[k].get("key_holdings") or ""):
                    snippet = merged[k].get("full_text_snippet") or r.get("full_text_snippet") or ""
                    merged[k] = dict(r)
                    merged[k]["full_text_snippet"] = snippet
                if r.get("full_text_snippet") and not merged[k].get("full_text_snippet"):
                    merged[k]["full_text_snippet"] = r["full_text_snippet"]

    _incorporate(sq, weight=3.0)
    _incorporate(ns, weight=1.5)
    _incorporate(nf, weight=1.5)

    # Enrich neo4j-only hits with SQLite snippet via reporter lookup
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for k, r in merged.items():
        if r.get("full_text_snippet"):
            continue
        rep = (r.get("reporter") or "").strip()
        if rep and rep not in ("U.S.", ""):
            cur.execute(
                "SELECT SUBSTR(full_text,1,3000) FROM precedents WHERE reporter=?",
                (rep,)
            )
            row = cur.fetchone()
            if row and row[0]:
                r["full_text_snippet"] = row[0]
    conn.close()

    ranked = sorted(merged.values(), key=lambda r: score_map.get(_result_key(r), 0), reverse=True)
    return ranked[:limit]
