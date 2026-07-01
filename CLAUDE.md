# CLAUDE.md — Legal Research Memory System

## What this project is

A self-strengthening legal research memory system for SCOTUS precedents.
Agents query a local structured database and knowledge graph first; when local
coverage is weak the system reaches out to Oyez and the Library of Congress,
ingests new authorities, and raises local coverage so fewer external calls are
needed over time.

Working directory: `C:/Users/davel/lq-assess-work/ASMT-02b2dbd7/`

---

## Components

### SQLite database (`db/`)

Single `precedents` table at `db/precedents.db`. Key columns:

- `bluebook_citation` — NOT NULL UNIQUE; used as the ingest dedup key
- `reporter` — `NNN U.S. PPP` for cases with formal citations; `"U.S."` for
  recent cases not yet assigned a volume/page
- `key_holdings` — Oyez conclusion summary text (short; sometimes empty for
  older cases)
- `full_text` — LOC PDF opinion text, up to 80,000 chars (pre-~2021 cases only)
- `key_arguments` — Oyez facts + question presented
- `subject_matter_type` — e.g. `"antitrust"`

Scripts:
- `python db/init_db.py` — create schema
- `python db/ingest.py` — walk `input/*.json`, INSERT OR IGNORE

### Neo4j knowledge graph (`graph/`)

Bolt at `bolt://localhost:7687` (password: `password`).

Node types: `Case`, `Judge`, `Session`, `Query`, `Response`, `WorkProduct`

Case unique key is `oyez_url` (not `reporter` — 35 of 50 cases have
`reporter = "U.S."`).

Case→Case edges extracted from `full_text` via `NNN U.S. PPP` regex:
`CITES`, `OVERRULES` (overrul* within 120 chars), `AFFIRMS` (affirm* within
120 chars).

Judge→Case edges from Oyez vote data: `AUTHORED_MAJORITY`, `JOINED_MAJORITY`,
`DISSENTED`, `CONCURRED`. Oyez vote type is `"majority"` / `"minority"` (not
`"dissent"`).

Scripts:
- `python graph/enrich_votes.py` — re-fetch vote data from Oyez → update input/ JSON
- `python graph/build_graph.py` — build Case + Judge nodes + edges
- `python graph/schema_queries.py` — apply indices for Session/Query/Response/WorkProduct

### Semantic cache (`graph/embed.py`, `graph/query_layer.py`)

Model: `sentence-transformers/all-MiniLM-L6-v2` (384-dim, runs locally).
Reuse threshold: cosine ≥ 0.82 (tunable via `REUSE_THRESHOLD` in `query_layer.py`).

API:
```python
start_session(user_id) -> session_id
log_query(session_id, text) -> (query_id, reuse_hit | None)
log_response(query_id, text, cited_case_urls, reused_from_query_id) -> response_id
log_work_product(session_id, content, wp_type, response_ids, cited_case_urls) -> wp_id
end_session(session_id)
get_reuse_stats() -> dict
```

### Live search + ingest pipeline (`pipeline/`)

- `pipeline/search_ingest.py` — coverage check → Oyez sweep → LOC PDF → SQLite + Neo4j
  - Coverage check uses `key_holdings` only (full_text excluded to prevent false inflation)
  - `COVERAGE_THRESHOLD = 0.50`, `MAX_NEW_CASES = 10`
  - Run as module: `python -m pipeline.search_ingest`
- `pipeline/full_text.py` — backfills LOC PDF full opinions for cases with
  formal reporters. URL pattern:
  `https://tile.loc.gov/storage-services/service/ll/usrep/usrep{vol}/usrep{vol}{page:03d}/usrep{vol}{page:03d}.pdf`

### Research agent (`legal_research_agent.md`)

Describes the full agent workflow:
1. Check semantic cache (reuse if similarity ≥ 0.82)
2. Trigger live ingest if local coverage < 0.50
3. Run `combined` retriever (union of all three approaches)
4. Draft legal memo with citations
5. Log response + work product to close the feedback loop

Use `from eval.retriever import combined` as the primary retrieval function.

### Evaluation harness (`eval/`)

- `eval/golden.json` — 7 hand-authored antitrust queries with expected citations
  and key holding phrases
- `eval/retriever.py` — four retrieval approaches: `sqlite_keyword`, `neo4j_subject`,
  `neo4j_fulltext`, `combined`
- `eval/agent_retriever.py` — multi-angle retrieval that simulates the agent's
  query decomposition + citation graph traversal (used by the agent eval)
- `eval/scorer.py` — citation recall/precision/F1, holding phrase coverage,
  composite score. Two entry points:
  - `score_results(gold, results)` — scores structured retriever output
  - `score_memo(gold, memo_text)` — scores free-form agent memo text directly
- `eval/run_eval.py` — agent benchmark orchestrated by Claude Code (see below)

---

## Agent Evaluation (`run_eval`)

The primary benchmark runs actual legal research agents against the golden
queries and scores their memo outputs. Claude Code is the orchestrator.

**To run the agent benchmark, tell Claude Code:**
> "Run the agent eval against eval/golden.json"

Claude Code will:

1. Read `eval/golden.json` to get the 7 queries.
2. For each query, spawn a sub-agent whose system prompt is the full contents
   of `legal_research_agent.md`. The agent receives the query and must:
   - Decompose it into search angles
   - Call `eval.retriever.combined()` across those angles
   - Follow citation edges via Neo4j
   - Return a memo with citations
3. Score each memo with `eval.scorer.score_memo(gold, memo_text)`.
4. Print a per-query summary (found/missing cases, holding phrase hits,
   composite score) and an overall mean composite.

**Scoring memo text:**
```python
from eval.scorer import score_memo
import json

with open("eval/golden.json") as f:
    golden = json.load(f)

memo = "... (agent-produced memo text) ..."
scores = score_memo(golden[0], memo)
# scores: citation_recall, citation_precision, citation_f1,
#         holding_coverage, composite
```

The scorer looks for expected reporter strings (`NNN U.S. PPP`) and key
holding phrases directly in the memo text, so any well-formed citation in the
memo counts as a hit.

**What a good score looks like:**

| composite | Meaning |
|---|---|
| 0.70+ | Right cases cited with accurate holdings |
| 0.50–0.69 | Right cases found, holding text incomplete |
| 0.30–0.49 | Some right cases, some missing |
| < 0.30 | Significant retrieval or coverage gap |

---

## How to run

### Prerequisites

```
Neo4j running at bolt://localhost:7687  (password: password)
  start: <neo4j_dir>/bin/neo4j.bat console

Python packages:
  pip install beautifulsoup4 lxml requests sentence-transformers pdfplumber neo4j
```

### Quick test sequence

```bash
# 1. Smoke test — validates Neo4j, embeddings, reuse detection
python graph/smoke_test.py

# 3. Live pipeline test
python -m pipeline.search_ingest
```

```
# 2. Agent benchmark (Claude Code orchestrates; tell Claude Code):
"Run the agent eval against eval/golden.json"
```

### Full rebuild from scratch

```bash
python db/init_db.py
python db/ingest.py
python pipeline/full_text.py
# Clear Neo4j first: MATCH (n) DETACH DELETE n
python graph/enrich_votes.py
python graph/build_graph.py
python graph/schema_queries.py
python eval/run_eval.py
```

---

## Common gotchas

- **`python pipeline/search_ingest.py` fails** with ModuleNotFoundError — use
  `python -m pipeline.search_ingest` instead
- **Oyez vote types** are `"majority"` / `"minority"`, not `"majority"` / `"dissent"`
- **Neo4j unique key** is `oyez_url`, not `reporter` — many recent cases share
  `reporter = "U.S."`
- **Coverage check** excludes `full_text` intentionally; 80k-char opinions match
  any keyword and would always return coverage = 1.0
- **Unicode on Windows** — use ASCII equivalents for `→`, `≥`, `…` in print
  statements when targeting cp1252 terminals

---

## Known limitations

1. Cases after ~2021 have no formal US Reports citation; LOC PDF unavailable.
2. Oyez has no topic search API — pipeline sweeps full term listings client-side.
3. Case→case citation edges sparse; Oyez summaries rarely contain `NNN U.S. PPP`.
4. Neo4j does not store `full_text`; `sqlite_keyword` and `combined` outperform
   pure Neo4j approaches because of LOC opinion access.
5. No LLM in the retrieval loop — a calling agent must supply response text to
   `log_response()`.
