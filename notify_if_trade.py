#!/usr/bin/env python3
"""Post-run check: if a new executed trade appeared since last check, fire a macOS notification.
Called by run_oos.sh after each run. Tracks last-seen trade id in data_real/last_notified_id.txt.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

GRAVEYARD = Path("data_real/graveyard.db")
WATERMARK = Path("data_real/last_notified_id.txt")

def notify(title: str, message: str) -> None:
    script = f'display notification "{message}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)

def main() -> None:
    if not GRAVEYARD.exists():
        return

    last_id = int(WATERMARK.read_text().strip()) if WATERMARK.exists() else 0

    conn = sqlite3.connect(str(GRAVEYARD))
    rows = conn.execute(
        "SELECT id, ticker, ev_pct, sized_usd, regime FROM trades "
        "WHERE outcome='executed' AND id > ? ORDER BY id",
        (last_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return

    for row_id, ticker, ev_pct, sized_usd, regime in rows:
        msg = f"{ticker} — EV {ev_pct:+.1f}%  ${sized_usd:.0f}  [{regime}]"
        print(f"[NOTIFY] trade executed: {msg}")
        notify("Hood Agent: Trade Executed ✓", msg)

    # Advance watermark to highest seen id
    WATERMARK.write_text(str(rows[-1][0]))

if __name__ == "__main__":
    main()
