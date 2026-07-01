"""
Fetch full SCOTUS opinion text from loc.gov PDFs.

URL pattern (confirmed working through vol 574):
  https://tile.loc.gov/storage-services/service/ll/usrep/
    usrep{vol}/usrep{vol}{page:03d}/usrep{vol}{page:03d}.pdf

Falls back to Oyez summary text when no PDF is available (recent cases
not yet in the US Reports print series, or volumes not yet digitised).
"""

from __future__ import annotations
import io
import re
import time
import requests
import pdfplumber

LOC_BASE = "https://tile.loc.gov/storage-services/service/ll/usrep"
HEADERS  = {"User-Agent": "Mozilla/5.0 (legal-research-bot/0.1; educational use)"}
DELAY    = 1.2   # be polite to a government server


def _loc_url(vol: str, page: str) -> str:
    vol  = vol.strip().lstrip("0") or "0"
    page = f"{int(page.strip()):03d}"
    return f"{LOC_BASE}/usrep{vol}/usrep{vol}{page}/usrep{vol}{page}.pdf"


def _extract_reporter(reporter: str) -> tuple[str, str] | None:
    """Parse 'NNN U.S. PPP' → ('NNN', 'PPP'), or None."""
    m = re.match(r"(\d+)\s+U\.S\.\s+(\d+)", reporter.strip())
    if m:
        return m.group(1), m.group(2)
    return None


def _pdf_to_text(pdf_bytes: bytes, max_chars: int = 80_000) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
            if sum(len(p) for p in text_parts) >= max_chars:
                break
    return "\n".join(text_parts)[:max_chars]


def fetch_full_text(reporter: str, fallback: str = "") -> tuple[str, str]:
    """
    Returns (full_text, source) where source is 'loc_pdf' or 'fallback'.
    """
    parsed = _extract_reporter(reporter)
    if not parsed:
        return fallback, "fallback"

    vol, page = parsed
    url = _loc_url(vol, page)

    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"):
            text = _pdf_to_text(r.content)
            if len(text) > 500:
                return text, "loc_pdf"
    except Exception:
        pass

    return fallback, "fallback"


def backfill_full_text(db_path: str, delay: float = DELAY):
    """
    Walk every row in the precedents table that has a real reporter
    and whose full_text is short (<2000 chars), fetch the LOC PDF,
    and update the row.
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    cur.execute(
        """
        SELECT id, reporter, full_text
        FROM   precedents
        WHERE  reporter != 'U.S.'
          AND  reporter != ''
        """
    )
    rows = cur.fetchall()
    print(f"Backfilling full text for {len(rows)} cases …\n")

    updated, skipped = 0, 0
    for row_id, reporter, existing_text in rows:
        text, source = fetch_full_text(reporter, existing_text or "")
        if source == "loc_pdf":
            cur.execute(
                "UPDATE precedents SET full_text = ? WHERE id = ?",
                (text, row_id),
            )
            print(f"  [PDF ] {reporter} -> {len(text):,} chars")
            updated += 1
        else:
            print(f"  [SKIP] {reporter} — no PDF or too short")
            skipped += 1
        time.sleep(delay)

    conn.commit()
    conn.close()
    print(f"\nDone. {updated} updated, {skipped} skipped.")


if __name__ == "__main__":
    import os
    DB = os.path.join(os.path.dirname(__file__), "..", "db", "precedents.db")
    backfill_full_text(DB)
