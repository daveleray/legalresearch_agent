import sqlite3, json, os, glob

DB_PATH = os.path.join(os.path.dirname(__file__), "precedents.db")
INPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "input")

FIELDS = [
    "bluebook_citation", "reporter", "subject_matter_type",
    "key_holdings", "venue_court", "judge", "winner",
    "full_text", "key_arguments",
]

def ingest():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.json")))
    inserted, skipped, errors = 0, 0, 0

    for path in files:
        fname = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            values = [rec.get(field, "") or "" for field in FIELDS]
            cur.execute(
                f"""INSERT OR IGNORE INTO precedents ({', '.join(FIELDS)})
                    VALUES ({', '.join(['?']*len(FIELDS))})""",
                values,
            )
            if cur.rowcount:
                inserted += 1
                print(f"  INS  {fname}")
            else:
                skipped += 1
                print(f"  SKIP {fname}  (duplicate citation)")
        except Exception as e:
            errors += 1
            print(f"  ERR  {fname} — {e}")

    conn.commit()
    conn.close()
    print(f"\nDone. {inserted} inserted, {skipped} skipped, {errors} errors.")

if __name__ == "__main__":
    ingest()
