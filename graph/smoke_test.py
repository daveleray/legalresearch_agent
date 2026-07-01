"""
End-to-end smoke test for the query layer.
Simulates two sessions: one original Q&A, one that reuses the answer.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graph.query_layer import (
    start_session, end_session,
    log_query, log_response, log_work_product,
    get_session_history, get_reuse_stats,
)

# ── Fetch a real Case oyez_url to use as a citation reference ─────────────────
from neo4j import GraphDatabase
d = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))
with d.session() as s:
    row = s.run(
        "MATCH (c:Case {subject_matter:'antitrust'}) RETURN c.oyez_url AS url, c.case_name AS name LIMIT 1"
    ).single()
    antitrust_url  = row["url"]  if row else None
    antitrust_name = row["name"] if row else "unknown"
d.close()

print(f"Using case: {antitrust_name}\n")

# ── Session 1: original query ─────────────────────────────────────────────────
print("=== Session 1: original query ===")
s1 = start_session(user_id="attorney_A")

q1_text = "What is the standard for monopolization under Section 2 of the Sherman Act?"
q1_id, reuse = log_query(s1, q1_text)
print(f"Query logged: {q1_id[:8]}…")
print(f"Reuse hit:   {reuse}")   # None on first run

r1_id = log_response(
    query_id=q1_id,
    text=(
        "Under Section 2 of the Sherman Act, monopolization requires proof of: "
        "(1) possession of monopoly power in the relevant market, and "
        "(2) the willful acquisition or maintenance of that power, as distinguished "
        "from growth or development as a consequence of a superior product, business "
        "acumen, or historical accident. See Aspen Skiing Co. v. Aspen Highlands "
        "Skiing Corp., 472 U.S. 585 (1985); Verizon Communications v. Trinko, "
        "540 U.S. 398 (2004)."
    ),
    cited_case_urls=[antitrust_url] if antitrust_url else [],
)
print(f"Response logged: {r1_id[:8]}…")

wp1_id = log_work_product(
    session_id=s1,
    content="MEMORANDUM: Section 2 Sherman Act analysis for client matter #1234...",
    wp_type="memo",
    response_ids=[r1_id],
    cited_case_urls=[antitrust_url] if antitrust_url else [],
    quality_notes="Reviewed and approved by supervising partner.",
)
print(f"Work product logged: {wp1_id[:8]}…")
end_session(s1)

# ── Session 2: semantically similar query — should get reuse hit ──────────────
print("\n=== Session 2: similar query (expect reuse hit) ===")
s2 = start_session(user_id="attorney_B")

q2_text = "What must a plaintiff prove to establish monopolization under Sherman Act Section 2?"
q2_id, reuse2 = log_query(s2, q2_text)
print(f"Query logged: {q2_id[:8]}…")

if reuse2:
    print(f"REUSE HIT (similarity={reuse2['similarity']}):")
    print(f"  Prior query response: {reuse2['response_text'][:120]}…")
    # Log the response, marking it as reused
    r2_id = log_response(
        query_id=q2_id,
        text=reuse2["response_text"],
        cited_case_urls=reuse2["cited_cases"],
        reused_from_query_id=reuse2["query_id"],
    )
else:
    print("No reuse hit — would need to generate fresh response.")
    r2_id = log_response(query_id=q2_id, text="[fresh response would go here]")

end_session(s2)

# ── Stats ─────────────────────────────────────────────────────────────────────
print("\n=== Reuse stats ===")
import json
print(json.dumps(get_reuse_stats(), indent=2))

print("\n=== Session 1 history ===")
for turn in get_session_history(s1):
    print(f"  Q: {turn['query_text'][:60]}")
    print(f"  A: {(turn['response_text'] or '')[:60]}…")
    print(f"  reused={turn['reused']}")
