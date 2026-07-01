# System Testing

## Overview

Testing falls into three categories:

| Category | What it checks | How to run |
|---|---|---|
| Unit / smoke | Individual components wire up correctly | `python graph/smoke_test.py` |
| Agent benchmark | Citation recall + holding accuracy on real agent memos | Tell Claude Code: "Run the agent eval against eval/golden.json" |
| Pipeline integration | Live Oyez search + ingest loop | `python -m pipeline.search_ingest` |

All commands are run from the working directory `C:/Users/davel/lq-assess-work/ASMT-02b2dbd7/`.

---

## Prerequisites

```
Neo4j running at bolt://localhost:7687  (password: password)
  start: <neo4j_dir>/bin/neo4j.bat console

Python packages:
  pip install beautifulsoup4 lxml requests sentence-transformers pdfplumber neo4j
```

---

## 1. Smoke Test — Query Layer

`graph/smoke_test.py` runs an end-to-end session cycle:

1. Opens Session 1, logs a Section 2 Sherman Act query, logs the response, logs a work product.
2. Opens Session 2 with a semantically similar query — expects a reuse hit (cosine ≥ 0.82).
3. Prints reuse stats and session history.

**Run:**
```
python graph/smoke_test.py
```

**Expected output (truncated):**
```
=== Session 1: original query ===
Query logged: <uuid>
Reuse hit:   None
Response logged: <uuid>
Work product logged: <uuid>

=== Session 2: similar query (expect reuse hit) ===
Query logged: <uuid>
REUSE HIT (similarity=0.84):
  Prior query response: Under Section 2 of the Sherman Act ...

=== Reuse stats ===
{
  "total_queries": 2,
  "reused_queries": 1,
  "total_responses": 2,
  "total_work_products": 1,
  "reuse_rate": 0.5
}
```

**What it validates:**
- Neo4j connection
- Session / Query / Response / WorkProduct node creation
- Embedding pipeline (sentence-transformers model loads and runs)
- Cosine similarity reuse detection at the 0.82 threshold
- REUSED_RESPONSE_FROM edge written on reuse

---

## 2. Agent Benchmark

### How it works

Claude Code orchestrates the benchmark. For each golden query, it spawns a
legal research agent whose system prompt is `legal_research_agent.md`. The
agent runs the full workflow: semantic cache check, coverage check + live
ingest if needed, multi-angle combined retrieval, citation graph traversal,
memo drafting. Each memo is saved to `eval/memos/<query_id>.txt`.

After all agents complete, run the scorer:

```
python eval/run_eval.py
```

This reads `eval/memos/` and scores each memo against the golden entry using
`eval/scorer.py`. Results are written to `eval/results.json`.

To score a single ad-hoc memo:

```
python eval/run_eval.py --query Q001 --memo "memo text here..."
```

### Golden Dataset

`eval/golden.json` — 7 antitrust queries with ground-truth citations and holding phrases:

| ID | Query | Expected cases |
|---|---|---|
| Q001 | Predatory pricing leading authorities | Brooke Group (509 U.S. 209), Weyerhaeuser (549 U.S. 312) |
| Q002 | Section 2 monopolization elements | Aspen Skiing (472 U.S. 585), Trinko (540 U.S. 398), Spectrum Sports (506 U.S. 447) |
| Q003 | Resale price maintenance / current standard | Leegin (551 U.S. 877) |
| Q004 | State action doctrine | NC Dental (574 U.S. 494), Phoebe Putney (568 U.S. 216) |
| Q005 | Two-sided markets antitrust analysis | Ohio v. Amex (585 U.S. 529), Apple v. Pepper |
| Q006 | Section 1 pleading standard | Twombly (550 U.S. 544) |
| Q007 | NCAA compensation restrictions | NCAA v. Alston (594 U.S. 69) |

Each golden entry contains:
- `expected_reporter_fragments` — `NNN U.S. PPP` strings searched in the memo text
- `expected_case_names` — name-based fallback for recent cases without formal citations
- `key_holding_phrases` — phrases that must appear in the memo for holding coverage credit

### Metrics

| Metric | Formula | What it tests |
|---|---|---|
| Citation recall | hits_found / expected_total | Did the memo cite the right cases? |
| Citation precision | expected_hits / total_cited | Are the memo's citations on point? |
| Citation F1 | 2 x P x R / (P + R) | Balanced citation accuracy |
| Holding coverage | phrases_found / phrases_total | Does the memo accurately describe the holdings? |
| Composite | (F1 + holding_coverage) / 2 | Overall quality |

### Score Interpretation

| Range | Meaning |
|---|---|
| 0.70+ | Good — right cases cited with accurate holding descriptions |
| 0.50–0.69 | Acceptable — right cases found but holding text incomplete |
| 0.30–0.49 | Partial — some right cases, some missing, holding gaps |
| < 0.30 | Weak — retrieval or coverage gap |

### Known data gaps that affect scores

**Q002 (Section 2 monopolization):**
Aspen Skiing (472 U.S. 585) has an empty `key_holdings` field — Oyez returned
no conclusion text for this 1993 case. The LOC full opinion is in the DB and
the agent should reach it via multi-angle retrieval and citation graph traversal,
but a well-written memo is the only reliable fix here. Backfilling `key_holdings`
from the LOC text would improve retrieval precision.

**Q007 (NCAA/Alston):**
Alston (2021) has no formal US Reports citation yet (`reporter = "U.S."`). The
scorer uses name-based matching as fallback so the case can still score a recall
hit, but LOC full opinion text is unavailable, limiting holding coverage.

**Q005 (two-sided markets):**
Both Ohio v. Amex and Apple v. Pepper lack formal US Reports citations. Name
matching handles recall; a knowledgeable agent memo should cover holding phrases
from its training knowledge of these cases.

---

## 3. Pipeline Integration Test

`pipeline/search_ingest.py` — tests the live Oyez search + ingest loop.

**Run:**
```
python -m pipeline.search_ingest
```

The `__main__` block runs a predatory pricing query. With the current 50-case DB:
- Coverage check returns 1.00 (Brooke Group and Weyerhaeuser already present in `key_holdings`)
- Oyez search is skipped
- Result confirms no new cases needed

To force a search on a topic not yet covered:
```python
from pipeline.search_ingest import search_and_ingest
search_and_ingest("merger HSR premerger Clayton Section 7", subject_matter="antitrust", force=True)
```

**What it validates:**
- Oyez multi-term listing fetch (14 terms × up to 100 cases)
- Keyword scoring with ≥ 2-hit threshold
- Deduplication against existing `input/` JSON files
- SQLite `INSERT OR IGNORE` idempotency
- Neo4j `MERGE` idempotency
- LOC PDF fetch and pdfplumber extraction
- Coverage delta reporting

---

## 4. Graph Integrity Checks

Run these Cypher queries directly in the Neo4j browser (`http://localhost:7474`) to verify the graph is intact after any rebuild:

```cypher
// Node counts
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC

// Expected: Case: 50+, Judge: 25+

// Relationship counts
MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC

// Expected: JOINED_MAJORITY: 330+, DISSENTED: 60+, AUTHORED_MAJORITY: 40+

// Verify judge with most majority opinions
MATCH (j:Judge)-[:AUTHORED_MAJORITY]->(c:Case)
RETURN j.name, count(c) AS cnt ORDER BY cnt DESC LIMIT 5

// Verify a known dissent
MATCH (j:Judge {name: "Clarence Thomas"})-[:DISSENTED]->(c:Case)
RETURN c.case_name LIMIT 5

// Verify session/query layer is populated
MATCH (s:Session)-[:HAS_QUERY]->(q:Query)-[:ANSWERED_BY]->(r:Response)
RETURN s.session_id, q.text, r.reused LIMIT 5
```

---

## 5. Rebuilding from Scratch

If the DB or graph needs to be wiped and rebuilt:

```bash
# 1. Re-initialise SQLite
python db/init_db.py

# 2. Re-ingest all 50 cases from input/ JSON files
python db/ingest.py

# 3. Backfill LOC PDF full opinions (15 cases with formal reporters)
python pipeline/full_text.py

# 4. Clear Neo4j and rebuild graph
#    (run MATCH (n) DETACH DELETE n in Neo4j browser first)
python graph/enrich_votes.py    # re-fetch vote data from Oyez
python graph/build_graph.py     # rebuild Case + Judge nodes + edges

# 5. Apply query layer schema
python graph/schema_queries.py

# 6. Verify with agent benchmark
# Tell Claude Code: "Run the agent eval against eval/golden.json"
# Then score the resulting memos:
python eval/run_eval.py
```

Total time: approximately 8–12 minutes (dominated by Oyez API rate limiting and LOC PDF downloads).

---

## 6. Adding New Golden Queries

Edit `eval/golden.json` and add an entry following the existing schema:

```json
{
  "query_id": "Q008",
  "query": "Your question here",
  "subject_matter": "antitrust",
  "expected_case_names": ["Full Case Name v. Other Party"],
  "expected_reporter_fragments": ["NNN U.S. PPP"],
  "key_holding_phrases": [
    "phrase that must appear in returned holding text",
    "another key phrase"
  ],
  "notes": "Explanation of what the correct answer requires."
}
```

Then tell Claude Code to run the agent eval. A composite below 0.50 on a new query signals a data or retrieval gap worth investigating.
