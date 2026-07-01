"""
Score agent-produced memos against the golden query set.

Usage
-----
After Claude Code has run each golden query through the legal research agent
and saved the resulting memos to eval/memos/<query_id>.txt, run:

  python eval/run_eval.py

The scorer looks for reporter fragments and key holding phrases in each memo
and prints per-query results + an overall mean composite score.

To score a single memo ad-hoc:
  python eval/run_eval.py --query Q001 --memo "memo text here..."
"""

import argparse, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.scorer import score_memo

GOLDEN_PATH  = os.path.join(os.path.dirname(__file__), "golden.json")
MEMOS_DIR    = os.path.join(os.path.dirname(__file__), "memos")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results.json")


def _score_all(golden: list[dict]) -> list[dict]:
    results = []
    for gold in golden:
        qid      = gold["query_id"]
        memo_path = os.path.join(MEMOS_DIR, f"{qid}.txt")
        if not os.path.exists(memo_path):
            print(f"  [{qid}] no memo found at {memo_path} — skipping")
            continue
        with open(memo_path, encoding="utf-8") as f:
            memo_text = f.read()
        scores = score_memo(gold, memo_text)
        results.append({"query_id": qid, "query": gold["query"], "scores": scores})
    return results


def _print_results(results: list[dict]) -> float:
    print(f"\n{'='*72}")
    print(f"  AGENT BENCHMARK RESULTS")
    print(f"{'='*72}")
    composites = []
    for r in results:
        s = r["scores"]
        print(f"\n[{r['query_id']}] {r['query'][:70]}")
        print(f"  recall={s['citation_recall']:.3f}  precision={s['citation_precision']:.3f}  "
              f"F1={s['citation_f1']:.3f}  "
              f"holding={s['holding_coverage']:.0%} ({s['phrase_hits']}/{s['phrase_total']})  "
              f"composite={s['composite']:.3f}")
        composites.append(s["composite"])
    mean = sum(composites) / len(composites) if composites else 0.0
    print(f"\n{'='*72}")
    print(f"  MEAN COMPOSITE: {mean:.3f}  (over {len(composites)} queries)")
    print(f"{'='*72}\n")
    return mean


def run_all():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)

    os.makedirs(MEMOS_DIR, exist_ok=True)
    results = _score_all(golden)

    if not results:
        print("No memos found. Run the agent benchmark first:\n"
              "  Tell Claude Code: 'Run the agent eval against eval/golden.json'")
        return

    mean = _print_results(results)

    output = {"mean_composite": round(mean, 3), "queries": results}
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"Full results -> {RESULTS_PATH}")


def run_single(query_id: str, memo_text: str):
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)
    gold = next((g for g in golden if g["query_id"] == query_id), None)
    if not gold:
        print(f"Query ID '{query_id}' not found in golden.json")
        return
    scores = score_memo(gold, memo_text)
    print(f"\n[{query_id}] {gold['query']}")
    print(f"  recall={scores['citation_recall']:.3f}  "
          f"precision={scores['citation_precision']:.3f}  "
          f"F1={scores['citation_f1']:.3f}  "
          f"holding={scores['holding_coverage']:.0%} ({scores['phrase_hits']}/{scores['phrase_total']})  "
          f"composite={scores['composite']:.3f}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", help="Score a single query by ID (requires --memo)")
    parser.add_argument("--memo",  help="Memo text to score (use with --query)")
    args = parser.parse_args()

    if args.query and args.memo:
        run_single(args.query, args.memo)
    else:
        run_all()
