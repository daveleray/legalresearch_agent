# System Design

## Purpose

A self-strengthening legal research memory system for SCOTUS precedents. Agents query a local structured database and knowledge graph first; when local coverage is weak the system reaches out to Oyez and the Library of Congress, ingests new authorities, and raises local coverage so fewer external calls are needed over time.

---

## High-Level Architecture

```
User / Agent Query
        │
        ▼
┌──────────────────────────────────────────┐
│         Legal Research Agent             │
│         (legal_research_agent.md)        │
│                                          │
│  1. Check semantic cache (reuse ≥ 0.82)  │
│  2. Coverage check → Oyez ingest if low  │
│  3. Multi-angle retrieval + graph walk   │
│  4. Draft memo with citations            │
│  5. Log response + work product          │
└───────────┬──────────────────────────────┘
            │  calls
            ▼
┌───────────────────┐
│  search_ingest.py │  ← coverage check → Oyez search → LOC PDF → ingest
│  (pipeline layer) │
└────────┬──────────┘
         │  results
         ▼
┌───────────────────────────────────────────────────────┐
│                    Neo4j Graph                        │
│                                                       │
│  (:Case)──[:CITES/OVERRULES/AFFIRMS]──▶(:Case)       │
│  (:Judge)──[:AUTHORED_MAJORITY/JOINED/DISSENTED]──▶  │
│  (:Session)──[:HAS_QUERY]──▶(:Query)                 │
│  (:Query)──[:ANSWERED_BY]──▶(:Response)              │
│  (:Response)──[:CITES_CASE]──▶(:Case)                │
│  (:Query)──[:SEMANTICALLY_SIMILAR]──▶(:Query)        │
│  (:Query)──[:REUSED_RESPONSE_FROM]──▶(:Query)        │
│  (:WorkProduct)──[:DERIVED_FROM]──▶(:Response)       │
│  (:WorkProduct)──[:REFERENCES_CASE]──▶(:Case)        │
└───────────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────┐
│                   SQLite DB                           │
│   precedents table (50+ rows, grows per query)        │
│   full_text up to 80,000 chars per case (LOC PDF)     │
└───────────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────┐
│   Semantic Cache  │  sentence-transformers all-MiniLM-L6-v2
│   (embed.py)      │  cosine similarity ≥ 0.82 → reuse prior answer
└───────────────────┘
```

---

## Components

### 1. Data Ingestion

**Scrapers** (`scraper/`)

| File | Purpose |
|---|---|
| `fetch_justia.py` | Downloads SCOTUS cases from the Oyez public REST API. Originally targeted Justia (403 blocked); rewritten to use `https://api.oyez.org`. Fetches case metadata, conclusion text, vote data, and justice lineup. |
| `fetch_antitrust.py` | Fetches 20 landmark antitrust SCOTUS decisions by known Oyez case paths (hardcoded list). Sets `subject_matter_type = "antitrust"`. |

Both scrapers write individual JSON files to `input/`.

**Vote enrichment** (`graph/enrich_votes.py`)

Re-fetches Oyez `decisions[].votes[]` for each case. Oyez uses `"majority"` / `"minority"` (not `"dissent"`) as vote type values. Writes `majority_justices`, `dissenting_justices`, `concurring_justices` arrays back into each `input/` JSON file.

**Full text** (`pipeline/full_text.py`)

Fetches full SCOTUS opinions as PDFs from the Library of Congress:

```
https://tile.loc.gov/storage-services/service/ll/usrep/
  usrep{vol}/usrep{vol}{page:03d}/usrep{vol}{page:03d}.pdf
```

Uses `pdfplumber` to extract text (up to 80,000 chars per opinion). Only available for cases with formal US Reports citations (pre-~2022). Falls back to Oyez summary text for recent cases not yet in the print series.

---

### 2. SQLite Database (`db/`)

Single table. Schema:

```sql
CREATE TABLE precedents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bluebook_citation   TEXT NOT NULL UNIQUE,
    reporter            TEXT,           -- e.g. "550 U.S. 544"
    subject_matter_type TEXT,           -- e.g. "antitrust"
    key_holdings        TEXT,           -- Oyez conclusion summary
    venue_court         TEXT,
    judge               TEXT,           -- majority opinion author
    winner              TEXT,           -- "petitioner" / "respondent" / "unknown"
    full_text           TEXT,           -- LOC PDF text, up to 80k chars
    key_arguments       TEXT,           -- Oyez facts + question
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

Ingest script: `db/ingest.py` — uses `INSERT OR IGNORE` on `bluebook_citation` for idempotency.

---

### 3. Neo4j Knowledge Graph (`graph/`)

**Case + Judge nodes** (`build_graph.py`)

Unique key: `oyez_url` (avoids the collision problem — many recent cases have no formal US Reports citation yet and would all merge to reporter `"U.S."`).

Node properties:

| Node | Key properties |
|---|---|
| `Case` | `oyez_url`, `bluebook_citation`, `case_name`, `reporter`, `subject_matter`, `venue_court`, `winner`, `key_holdings`, `key_arguments` |
| `Judge` | `name` |

**Case → Case relationships** (extracted from `full_text` + `key_holdings` via regex):

| Relationship | Detection |
|---|---|
| `CITES` | `NNN U.S. PPP` pattern found in text, no overrule/affirm keyword nearby |
| `OVERRULES` | `overrul*` within 120 chars of the cited reporter string |
| `AFFIRMS` | `affirm*` within 120 chars of the cited reporter string |

**Judge → Case relationships** (from Oyez vote data):

| Relationship | Source |
|---|---|
| `AUTHORED_MAJORITY` | `written_opinion[type=majority].judge_full_name` |
| `JOINED_MAJORITY` | `decisions[].votes[vote="majority"]` |
| `DISSENTED` | `decisions[].votes[vote="minority"]` |
| `CONCURRED` | `decisions[].votes[vote~"concurr"]` |

**Query / Response / WorkProduct layer** (`query_layer.py`, `schema_queries.py`)

Tracks agent memory across sessions:

| Node | Key properties |
|---|---|
| `Session` | `session_id`, `user_id`, `started_at`, `ended_at` |
| `Query` | `query_id`, `text`, `timestamp`, `embedding` (384-dim JSON array) |
| `Response` | `response_id`, `text`, `timestamp`, `cited_cases`, `reused` |
| `WorkProduct` | `wp_id`, `content`, `wp_type`, `timestamp`, `quality_notes` |

Additional relationships:

| Relationship | Meaning |
|---|---|
| `Session → HAS_QUERY → Query` | Session contains this query |
| `Query → ANSWERED_BY → Response` | The response to a query |
| `Response → CITES_CASE → Case` | Response cited these precedents |
| `Query → SEMANTICALLY_SIMILAR {score} → Query` | Embedding cosine similarity link |
| `Query → REUSED_RESPONSE_FROM → Query` | This query reused a prior answer |
| `WorkProduct → DERIVED_FROM → Response` | Work product built from these responses |
| `WorkProduct → REFERENCES_CASE → Case` | Final deliverable cited these cases |

---

### 4. Semantic Cache (`graph/embed.py`, `graph/query_layer.py`)

Model: `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~80 MB, runs locally).

On every `log_query()` call:
1. Embed the incoming query text.
2. Fetch all past `Query` nodes that have a linked `Response`.
3. Compute cosine similarity against each past embedding (Python-side; fine at corpus scale).
4. If best score ≥ 0.82: surface the cached response as a reuse candidate.
5. Write a `SEMANTICALLY_SIMILAR {score}` edge regardless of threshold.
6. If the caller accepts the reuse: write a `REUSED_RESPONSE_FROM` edge.

Threshold (0.82) is tunable in `query_layer.py → REUSE_THRESHOLD`.

---

### 5. Live Search + Ingest Pipeline (`pipeline/search_ingest.py`)

Called before every retrieval when an agent query arrives.

```
1. _local_coverage(query)
      keyword overlap against key_holdings only
      (full_text excluded — 80k-char opinions inflate coverage falsely)
      normalised: min(matching_cases / 3, 1.0)

2. If coverage < 0.50 (COVERAGE_THRESHOLD):
      _search_oyez(query)
        → fetch up to 14 recent SCOTUS terms from Oyez API
        → score each case stub by keyword overlap on name + description + question
        → require ≥ 2 distinct keyword hits (avoids generic-term false positives)
        → return top 20 candidates not already in DB

3. For each candidate (up to MAX_NEW_CASES = 10):
      _fetch_oyez_case()   → full Oyez detail + vote data
      fetch_full_text()    → LOC PDF if reporter is available
      _ingest_sqlite()     → INSERT OR IGNORE
      _ingest_neo4j()      → MERGE Case + Judge edges
      _save_json()         → write to input/ for audit trail

4. Re-run _local_coverage() → report coverage delta
```

Over time: as the DB grows, coverage scores rise and Oyez round-trips decrease.

---

### 6. Evaluation Harness (`eval/`)

| File | Purpose |
|---|---|
| `golden.json` | 7 hand-authored antitrust queries with expected citations, reporter fragments, and key holding phrases |
| `retriever.py` | Four underlying retrieval functions: `sqlite_keyword`, `neo4j_subject`, `neo4j_fulltext`, `combined` |
| `scorer.py` | Citation recall/precision/F1, holding phrase coverage, composite score. `score_memo(gold, text)` scores free-form memo text directly. |
| `run_eval.py` | Scores agent-produced memos in `eval/memos/<query_id>.txt` against the golden set. Also accepts `--query`/`--memo` flags for one-off scoring. |

**How the benchmark runs:**

Claude Code acts as orchestrator. For each golden query it spawns a legal
research agent (system prompt: `legal_research_agent.md`), which runs the full
workflow — semantic cache check, coverage check + ingest, multi-angle combined
retrieval, citation graph traversal, memo drafting. The agent saves its memo to
`eval/memos/<query_id>.txt`. Then `python eval/run_eval.py` scores all memos.

**Scoring (applied to memo text):**

- **Citation recall** — fraction of expected `NNN U.S. PPP` strings (or case names for uncited recent cases) found anywhere in the memo
- **Citation precision** — fraction of cited reporters in the memo that are expected
- **Citation F1** — harmonic mean
- **Holding coverage** — fraction of golden key phrases found in the memo text
- **Composite** — equal-weight average of F1 and holding coverage

**Retrieval functions** (`retriever.py`) are the tools the agent calls — they
are not the eval unit themselves:

| Function | Description |
|---|---|
| `sqlite_keyword` | OR-based LIKE across `key_holdings`, `key_arguments`, `full_text`; scored 3/2/2 |
| `neo4j_subject` | Case nodes filtered by `subject_matter`, ranked by keyword overlap |
| `neo4j_fulltext` | Lucene fulltext index over `key_holdings`, `key_arguments`, `case_name` |
| `combined` | Union of all three, deduplicated and re-ranked; enriches neo4j hits with SQLite `full_text_snippet` |

---

## Data Flow: Single Query

```
Agent receives: "What are the leading authorities for predatory pricing?"
        │
        ▼
log_query(session_id, text)
  ├── embed query → 384-dim vector
  ├── search past Query nodes for cosine similarity >= 0.82
  └── return (query_id, reuse_hit | None)
        │
        ├─ reuse_hit? → surface cached response → done
        │
        └─ no hit:
                │
                ▼
        search_and_ingest(query)
          ├── coverage check: keyword overlap vs key_holdings
          ├── coverage >= 0.50? → skip Oyez
          └── coverage < 0.50? → sweep Oyez → fetch LOC PDFs → ingest
                │
                ▼
        Agent decomposes query into doctrinal search angles
          e.g. "below-cost pricing recoupment", "predatory bidding",
               "Brooke Group Weyerhaeuser", individual keywords, pairs
                │
                ▼
        combined(angle, ...) for each angle → merge + dedup
          ├── sqlite_keyword  (full 80k-char opinion text)
          ├── neo4j_subject   (subject matter filter + keyword rank)
          └── neo4j_fulltext  (Lucene index)
                │
                ▼
        Follow CITES/OVERRULES/AFFIRMS edges from found cases
          ├── upstream: foundational authorities cited by found cases
          └── downstream: later cases that cite or limit found cases
                │
                ▼
        Draft memo with citations
                │
                ▼
        log_response(query_id, memo_text, cited_case_urls)
          ├── create Response node
          ├── CITES_CASE → relevant Case nodes
          └── REUSED_RESPONSE_FROM if applicable
                │
                ▼
        log_work_product(session_id, memo_text, ...)
          ├── DERIVED_FROM → Response nodes
          └── REFERENCES_CASE → Case nodes
```

---

## Known Limitations

1. **Recent cases lack formal citations.** Oyez does not yet have US Reports volume/page for cases decided after ~2021. The `reporter` field stores `"U.S."` for these, making citation-based matching impossible and preventing LOC PDF download.

2. **Oyez has no topic search API.** The live search strategy fetches entire term listings (up to 100 cases per term × 14 terms) and filters client-side. This is bandwidth-heavy and misses cases whose party names don't contain topic keywords.

3. **Case-to-case citation edges are sparse.** Oyez summary text rarely contains `NNN U.S. PPP` citation strings. The citation graph will deepen as full LOC opinion text is indexed and a proper citation parser is added.

4. **Neo4j full text not yet indexed.** The `full_text` field is stored in SQLite but not written to Neo4j `Case` nodes, so `neo4j_fulltext` and `neo4j_subject` retrievers cannot benefit from it. The `sqlite_keyword` approach currently outperforms both Neo4j approaches as a result.

5. **Agent supplies the response text.** The system retrieves, ranks, and caches; it does not generate memo text. The legal research agent (an LLM) reads retrieved results, drafts the memo, and passes the text back to `log_response()` to close the feedback loop.
