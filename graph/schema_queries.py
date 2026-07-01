"""
Adds Query/Response/WorkProduct/Session nodes and indices to Neo4j.
Safe to run multiple times (uses IF NOT EXISTS).
"""

SCHEMA_STATEMENTS = [
    # Indices
    "CREATE INDEX session_id IF NOT EXISTS FOR (s:Session) ON (s.session_id)",
    "CREATE INDEX query_id   IF NOT EXISTS FOR (q:Query)   ON (q.query_id)",
    "CREATE INDEX response_id IF NOT EXISTS FOR (r:Response) ON (r.response_id)",
    "CREATE INDEX wp_id      IF NOT EXISTS FOR (w:WorkProduct) ON (w.wp_id)",
]

# Relationship types used:
#   (:Session)-[:HAS_QUERY]->(:Query)
#   (:Query)-[:ANSWERED_BY]->(:Response)
#   (:Response)-[:CITES_CASE]->(:Case)
#   (:Query)-[:SEMANTICALLY_SIMILAR {score}]->(:Query)
#   (:Query)-[:REUSED_RESPONSE_FROM]->(:Query)    # this query reused answer from that one
#   (:WorkProduct)-[:DERIVED_FROM]->(:Response)
#   (:WorkProduct)-[:REFERENCES_CASE]->(:Case)
#   (:Session)-[:HAS_WORK_PRODUCT]->(:WorkProduct)


def apply(driver):
    with driver.session() as s:
        for stmt in SCHEMA_STATEMENTS:
            s.run(stmt)
    print("Schema applied.")


if __name__ == "__main__":
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))
    apply(driver)
    driver.close()
