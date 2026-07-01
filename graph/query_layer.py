"""
Knowledge graph layer for user queries, responses, and work products.

Public API
----------
start_session(user_id)          -> session_id
log_query(session_id, text)     -> query_id, reuse_hit | None
log_response(query_id, text,
             cited_case_urls,
             reused_from_query_id)  -> response_id
log_work_product(session_id,
                 content, wp_type,
                 response_ids,
                 cited_case_urls,
                 quality_notes)     -> wp_id
end_session(session_id)
get_session_history(session_id) -> list[dict]

Reuse hit schema
----------------
{
  "query_id":    str,       # the past query
  "response_id": str,       # its cached response
  "response_text": str,
  "similarity":  float,
  "cited_cases": list[str],
}
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from typing import Optional

from neo4j import GraphDatabase

from .embed import embed, cosine_similarity

NEO4J_URI      = "bolt://localhost:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "password"
REUSE_THRESHOLD = 0.82   # cosine similarity above which we surface a cached answer


def _driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Session ───────────────────────────────────────────────────────────────────

def start_session(user_id: str = "default") -> str:
    session_id = str(uuid.uuid4())
    driver = _driver()
    with driver.session() as s:
        s.run(
            """
            CREATE (sess:Session {
                session_id: $session_id,
                user_id:    $user_id,
                started_at: $ts,
                ended_at:   null
            })
            """,
            session_id=session_id, user_id=user_id, ts=_now(),
        )
    driver.close()
    return session_id


def end_session(session_id: str):
    driver = _driver()
    with driver.session() as s:
        s.run(
            "MATCH (sess:Session {session_id:$sid}) SET sess.ended_at = $ts",
            sid=session_id, ts=_now(),
        )
    driver.close()


# ── Query ─────────────────────────────────────────────────────────────────────

def log_query(
    session_id: str,
    text: str,
) -> tuple[str, Optional[dict]]:
    """
    Create a Query node, link it to the session, check for a reuse hit.

    Returns (query_id, reuse_hit | None).
    """
    query_id  = str(uuid.uuid4())
    embedding = embed(text)

    driver = _driver()
    with driver.session() as s:
        s.run(
            """
            MATCH (sess:Session {session_id: $sid})
            CREATE (q:Query {
                query_id:  $qid,
                text:      $text,
                timestamp: $ts,
                embedding: $emb
            })
            CREATE (sess)-[:HAS_QUERY]->(q)
            """,
            sid=session_id, qid=query_id, text=text,
            ts=_now(), emb=json.dumps(embedding),
        )

    reuse = _find_reuse_candidate(driver, query_id, embedding)

    if reuse:
        # Record the semantic similarity link
        with driver.session() as s:
            s.run(
                """
                MATCH (a:Query {query_id:$qid})
                MATCH (b:Query {query_id:$src_qid})
                MERGE (a)-[:SEMANTICALLY_SIMILAR {score: $score}]->(b)
                """,
                qid=query_id, src_qid=reuse["query_id"], score=reuse["similarity"],
            )

    driver.close()
    return query_id, reuse


def _find_reuse_candidate(driver, exclude_query_id: str, embedding: list[float]) -> Optional[dict]:
    """
    Fetch all past Query nodes that have a linked Response, compute cosine
    similarity, return the best hit above REUSE_THRESHOLD.
    """
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (q:Query)-[:ANSWERED_BY]->(r:Response)
            WHERE q.query_id <> $exclude
              AND q.embedding IS NOT NULL
            RETURN q.query_id   AS query_id,
                   q.embedding  AS embedding,
                   r.response_id AS response_id,
                   r.text        AS response_text,
                   r.cited_cases AS cited_cases
            """,
            exclude=exclude_query_id,
        ).data()

    best = None
    best_score = REUSE_THRESHOLD - 0.001

    for row in rows:
        try:
            past_emb = json.loads(row["embedding"])
        except Exception:
            continue
        score = cosine_similarity(embedding, past_emb)
        if score > best_score:
            best_score = score
            best = {
                "query_id":     row["query_id"],
                "response_id":  row["response_id"],
                "response_text": row["response_text"] or "",
                "cited_cases":  json.loads(row["cited_cases"] or "[]"),
                "similarity":   round(score, 4),
            }

    return best


# ── Response ──────────────────────────────────────────────────────────────────

def log_response(
    query_id: str,
    text: str,
    cited_case_urls: list[str] | None = None,
    reused_from_query_id: str | None = None,
) -> str:
    response_id = str(uuid.uuid4())
    cited = cited_case_urls or []

    driver = _driver()
    with driver.session() as s:
        s.run(
            """
            MATCH (q:Query {query_id: $qid})
            CREATE (r:Response {
                response_id: $rid,
                text:        $text,
                timestamp:   $ts,
                cited_cases: $cited,
                reused:      $reused
            })
            CREATE (q)-[:ANSWERED_BY]->(r)
            """,
            qid=query_id, rid=response_id, text=text[:8000],
            ts=_now(), cited=json.dumps(cited), reused=reused_from_query_id is not None,
        )

        # Link response to Case nodes it cited
        for url in cited:
            s.run(
                """
                MATCH (r:Response {response_id: $rid})
                MATCH (c:Case {oyez_url: $url})
                MERGE (r)-[:CITES_CASE]->(c)
                """,
                rid=response_id, url=url,
            )

        # Mark reuse lineage
        if reused_from_query_id:
            s.run(
                """
                MATCH (new_q:Query  {query_id: $new_qid})
                MATCH (src_q:Query  {query_id: $src_qid})
                MERGE (new_q)-[:REUSED_RESPONSE_FROM]->(src_q)
                """,
                new_qid=query_id, src_qid=reused_from_query_id,
            )

    driver.close()
    return response_id


# ── Work Product ──────────────────────────────────────────────────────────────

def log_work_product(
    session_id: str,
    content: str,
    wp_type: str = "memo",
    response_ids: list[str] | None = None,
    cited_case_urls: list[str] | None = None,
    quality_notes: str = "",
) -> str:
    wp_id = str(uuid.uuid4())

    driver = _driver()
    with driver.session() as s:
        s.run(
            """
            MATCH (sess:Session {session_id: $sid})
            CREATE (w:WorkProduct {
                wp_id:         $wp_id,
                content:       $content,
                wp_type:       $wp_type,
                timestamp:     $ts,
                quality_notes: $notes
            })
            CREATE (sess)-[:HAS_WORK_PRODUCT]->(w)
            """,
            sid=session_id, wp_id=wp_id, content=content[:16000],
            wp_type=wp_type, ts=_now(), notes=quality_notes,
        )

        for rid in (response_ids or []):
            s.run(
                """
                MATCH (w:WorkProduct {wp_id: $wp_id})
                MATCH (r:Response    {response_id: $rid})
                MERGE (w)-[:DERIVED_FROM]->(r)
                """,
                wp_id=wp_id, rid=rid,
            )

        for url in (cited_case_urls or []):
            s.run(
                """
                MATCH (w:WorkProduct {wp_id: $wp_id})
                MATCH (c:Case {oyez_url: $url})
                MERGE (w)-[:REFERENCES_CASE]->(c)
                """,
                wp_id=wp_id, url=url,
            )

    driver.close()
    return wp_id


# ── Read / history ────────────────────────────────────────────────────────────

def get_session_history(session_id: str) -> list[dict]:
    driver = _driver()
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (sess:Session {session_id: $sid})-[:HAS_QUERY]->(q:Query)
            OPTIONAL MATCH (q)-[:ANSWERED_BY]->(r:Response)
            OPTIONAL MATCH (q)-[:REUSED_RESPONSE_FROM]->(src:Query)
            RETURN q.query_id   AS query_id,
                   q.text       AS query_text,
                   q.timestamp  AS query_ts,
                   r.response_id AS response_id,
                   r.text        AS response_text,
                   r.reused      AS reused,
                   src.query_id  AS reused_from
            ORDER BY q.timestamp
            """,
            sid=session_id,
        ).data()
    driver.close()
    return rows


def get_most_cited_cases(limit: int = 10) -> list[dict]:
    """Which Case nodes appear most often in Response CITES_CASE edges."""
    driver = _driver()
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (r:Response)-[:CITES_CASE]->(c:Case)
            RETURN c.case_name AS case_name,
                   c.bluebook_citation AS citation,
                   count(r) AS times_cited
            ORDER BY times_cited DESC
            LIMIT $limit
            """,
            limit=limit,
        ).data()
    driver.close()
    return rows


def get_reuse_stats() -> dict:
    driver = _driver()
    with driver.session() as s:
        total_q  = s.run("MATCH (q:Query) RETURN count(q) AS n").single()["n"]
        reused_q = s.run("MATCH (q:Query)-[:REUSED_RESPONSE_FROM]->() RETURN count(q) AS n").single()["n"]
        total_r  = s.run("MATCH (r:Response) RETURN count(r) AS n").single()["n"]
        total_wp = s.run("MATCH (w:WorkProduct) RETURN count(w) AS n").single()["n"]
    driver.close()
    return {
        "total_queries":   total_q,
        "reused_queries":  reused_q,
        "total_responses": total_r,
        "total_work_products": total_wp,
        "reuse_rate": round(reused_q / total_q, 3) if total_q else 0,
    }
