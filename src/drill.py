"""Flashcard-style study mode with SM-2 spaced repetition."""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import chess

from db import DB_PATH, get_connection, transaction

_DRILL_SCHEMA = """
CREATE TABLE IF NOT EXISTS drill_stats (
    id                 INTEGER PRIMARY KEY,
    move_id            INTEGER NOT NULL UNIQUE REFERENCES moves(id),
    last_reviewed      TEXT,
    times_seen         INTEGER NOT NULL DEFAULT 0,
    times_correct      INTEGER NOT NULL DEFAULT 0,
    ease_factor        REAL    NOT NULL DEFAULT 2.5,
    interval_days      INTEGER NOT NULL DEFAULT 1,
    due_date           TEXT,
    consecutive_correct INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_drill_move ON drill_stats(move_id);
CREATE TABLE IF NOT EXISTS drill_stops (
    id         INTEGER PRIMARY KEY,
    fen        TEXT NOT NULL,
    repertoire TEXT NOT NULL,
    UNIQUE(fen, repertoire)
);
"""

_DUE_MOVES_SQL = """
    SELECT m.id AS move_id, m.uci, m.san, m.frequency,
           pf.fen AS from_fen,
           COALESCE(ds.times_seen, 0)    AS times_seen,
           COALESCE(ds.times_correct, 0) AS times_correct,
           COALESCE(ds.ease_factor, 2.5) AS ease_factor,
           COALESCE(ds.interval_days, 1) AS interval_days,
           ds.due_date
    FROM moves m
    JOIN positions pf ON pf.id = m.from_position_id
    LEFT JOIN drill_stats ds ON ds.move_id = m.id
    WHERE m.repertoire = ?
      AND m.is_my_move = 1
      AND m.frequency >= 1
      AND (ds.due_date IS NULL OR ds.due_date <= ?)
    ORDER BY
        CASE WHEN ds.due_date IS NULL THEN 0 ELSE 1 END,
        ds.due_date ASC,
        m.frequency DESC
    LIMIT ?
"""


def init_drill_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(_DRILL_SCHEMA)
        for migration in (
            "ALTER TABLE moves ADD COLUMN prep_status TEXT",
            "ALTER TABLE drill_stats ADD COLUMN consecutive_correct INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(migration)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


_MAX_INTERVAL_DAYS = 365

def _sm2_next(ef: float, interval: int, correct: bool) -> tuple[float, int]:
    """SM-2 scheduling. Returns (new_ease_factor, new_interval_days)."""
    if correct:
        new_interval = 6 if interval <= 1 else round(interval * ef)
        new_ef = min(ef + 0.1, 3.0)
    else:
        new_interval = 1
        new_ef = max(ef - 0.2, 1.3)
    return new_ef, min(new_interval, _MAX_INTERVAL_DAYS)


def _record_result(conn: sqlite3.Connection, move_id: int, correct: bool, ef: float, interval: int) -> int:
    """Record result and return the updated consecutive_correct count."""
    today = date.today().isoformat()
    due = (date.today() + timedelta(days=interval)).isoformat()
    new_consecutive = "consecutive_correct + 1" if correct else "0"
    conn.execute(
        f"""
        INSERT INTO drill_stats
            (move_id, last_reviewed, times_seen, times_correct, ease_factor, interval_days, due_date,
             consecutive_correct)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?)
        ON CONFLICT(move_id) DO UPDATE SET
            last_reviewed       = excluded.last_reviewed,
            times_seen          = times_seen + 1,
            times_correct       = times_correct + excluded.times_correct,
            ease_factor         = excluded.ease_factor,
            interval_days       = excluded.interval_days,
            due_date            = excluded.due_date,
            consecutive_correct = {new_consecutive}
        """,
        (move_id, today, int(correct), ef, interval, due, 1 if correct else 0),
    )
    row = conn.execute(
        "SELECT consecutive_correct FROM drill_stats WHERE move_id = ?", (move_id,)
    ).fetchone()
    return row["consecutive_correct"] if row else (1 if correct else 0)


def auto_label_alternatives(db_path: Path = DB_PATH) -> int:
    """Auto-label unlabeled secondary my-moves as 'blue' (alternative).

    For every position that has 2+ my-moves, any unlabeled move whose frequency
    is strictly below the maximum frequency from that position is marked blue.
    Moves that already carry any label are never modified.

    Returns the number of moves labeled.
    """
    with transaction(db_path) as conn:
        result = conn.execute(
            """
            WITH multi_pos AS (
                SELECT from_position_id, repertoire, MAX(frequency) AS max_freq
                FROM moves
                WHERE is_my_move = 1
                GROUP BY from_position_id, repertoire
                HAVING COUNT(*) >= 2
            )
            UPDATE moves SET prep_status = 'blue'
            WHERE is_my_move = 1
              AND prep_status IS NULL
              AND id IN (
                  SELECT m.id
                  FROM moves m
                  JOIN multi_pos mp
                    ON m.from_position_id = mp.from_position_id
                   AND m.repertoire       = mp.repertoire
                  WHERE m.is_my_move = 1
                    AND m.frequency < mp.max_freq
              )
            """
        )
        return result.rowcount


def get_due_moves(repertoire: str, db_path: Path = DB_PATH, session_size: int = 20) -> list:
    """Return rows due for drill (public API for GUI use)."""
    init_drill_db(db_path)
    today = date.today().isoformat()
    conn = get_connection(db_path)
    try:
        return conn.execute(_DUE_MOVES_SQL, (repertoire, today, session_size)).fetchall()
    finally:
        conn.close()


_YELLOW_STREAK_THRESHOLD = 3


def record_result(move_id: int, correct: bool, ease_factor: float, interval_days: int,
                  db_path: Path = DB_PATH) -> bool:
    """Record a single drill result and update SM-2 stats.

    Returns True if the move was promoted from yellow to green (streak reached).
    """
    new_ef, new_interval = _sm2_next(ease_factor, interval_days, correct)
    with transaction(db_path) as conn:
        consecutive = _record_result(conn, move_id, correct, new_ef, new_interval)
        if correct and consecutive >= _YELLOW_STREAK_THRESHOLD:
            updated = conn.execute(
                "UPDATE moves SET prep_status = 'green' WHERE id = ? AND prep_status = 'yellow'",
                (move_id,),
            ).rowcount
            if updated:
                # Reset streak so the promotion doesn't fire again next review
                conn.execute(
                    "UPDATE drill_stats SET consecutive_correct = 0 WHERE move_id = ?",
                    (move_id,),
                )
                return True
    return False


def get_drill_stats(repertoire: str, db_path: Path = DB_PATH) -> dict:
    """Return aggregate drill statistics for the given repertoire."""
    init_drill_db(db_path)
    today = date.today().isoformat()
    conn = get_connection(db_path)
    try:
        total_moves = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE repertoire = ? AND is_my_move = 1", (repertoire,)
        ).fetchone()[0]
        due_now = conn.execute(
            """
            SELECT COUNT(*) FROM moves m
            LEFT JOIN drill_stats ds ON ds.move_id = m.id
            WHERE m.repertoire = ? AND m.is_my_move = 1
              AND (ds.due_date IS NULL OR ds.due_date <= ?)
            """,
            (repertoire, today),
        ).fetchone()[0]
        reviewed_today = conn.execute(
            "SELECT COUNT(*) FROM drill_stats WHERE last_reviewed = ?", (today,)
        ).fetchone()[0]
        correct_today = conn.execute(
            """
            SELECT COALESCE(SUM(times_correct), 0) FROM drill_stats ds
            JOIN moves m ON m.id = ds.move_id
            WHERE m.repertoire = ? AND ds.last_reviewed = ?
            """,
            (repertoire, today),
        ).fetchone()[0]
        return {
            "total_my_moves": total_moves,
            "due_now": due_now,
            "reviewed_today": reviewed_today,
            "correct_today": correct_today,
        }
    finally:
        conn.close()


def toggle_drill_stop(fen: str, repertoire: str, db_path: Path = DB_PATH) -> bool:
    """Add a drill stop if absent, remove it if present. Returns True if now a stop."""
    init_drill_db(db_path)
    conn = get_connection(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM drill_stops WHERE fen = ? AND repertoire = ?",
            (fen, repertoire),
        ).fetchone()
    finally:
        conn.close()
    with transaction(db_path) as conn:
        if exists:
            conn.execute(
                "DELETE FROM drill_stops WHERE fen = ? AND repertoire = ?",
                (fen, repertoire),
            )
            return False
        else:
            conn.execute(
                "INSERT OR IGNORE INTO drill_stops (fen, repertoire) VALUES (?, ?)",
                (fen, repertoire),
            )
            return True


def run_drill(
    repertoire: str,
    db_path: Path = DB_PATH,
    session_size: int = 20,
) -> None:
    """Interactive terminal drill session."""
    init_drill_db(db_path)
    today = date.today().isoformat()

    conn = get_connection(db_path)
    rows = conn.execute(_DUE_MOVES_SQL, (repertoire, today, session_size)).fetchall()
    conn.close()

    rows = list(rows)
    if not rows:
        print("No moves due for review. Come back later or run more games to build the tree.")
        return

    random.shuffle(rows)
    total = len(rows)
    correct_count = 0

    print(f"\n=== Drill Mode — {repertoire.capitalize()} Repertoire ===")
    print(f"Reviewing {total} position(s). Enter your move in SAN (e.g. Nf3) or UCI (e.g. g1f3).\n")

    reviewed = 0
    try:
        for i, row in enumerate(rows, 1):
            try:
                board = chess.Board(row["from_fen"] + " 0 1")
            except Exception:
                continue

            print(f"--- Position {i}/{total} ---")
            print(board)
            turn_label = "White" if board.turn == chess.WHITE else "Black"
            print(f"Turn: {turn_label}  |  Stats: {row['times_correct']}/{row['times_seen']} correct")
            print("(Ctrl+C to end session early)\n")

            try:
                answer = input("Your move: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            user_move: chess.Move | None = None
            try:
                user_move = board.parse_san(answer)
            except ValueError:
                try:
                    user_move = board.parse_uci(answer)
                except ValueError:
                    pass

            expected_uci = row["uci"]
            correct = user_move is not None and user_move.uci() == expected_uci

            if correct:
                print(f"  Correct! ({row['san']})")
                correct_count += 1
            else:
                print(f"  Incorrect. Expected: {row['san']}")

            new_ef, new_interval = _sm2_next(row["ease_factor"], row["interval_days"], correct)
            with transaction(db_path) as conn:
                _record_result(conn, row["move_id"], correct, new_ef, new_interval)

            due_str = (date.today() + timedelta(days=new_interval)).isoformat()
            print(f"  Next review: {due_str}")
            print()
            reviewed += 1

    except KeyboardInterrupt:
        print()

    if reviewed == 0:
        return
    pct = f"{correct_count / reviewed:.0%}"
    print(f"=== Session complete: {correct_count}/{reviewed} correct ({pct}) ===")
