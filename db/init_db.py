import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "precedents.db")

def init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS precedents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            bluebook_citation   TEXT NOT NULL UNIQUE,
            reporter            TEXT,
            subject_matter_type TEXT,
            key_holdings        TEXT,
            venue_court         TEXT,
            judge               TEXT,
            winner              TEXT,
            full_text           TEXT,
            key_arguments       TEXT,
            created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    print(f"Database initialised at {DB_PATH}")

if __name__ == "__main__":
    init()
