from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "repertoire.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id  INTEGER PRIMARY KEY,
    fen TEXT UNIQUE NOT NULL
);

-- Each row is one move edge in the opening tree.
-- repertoire: which color's game history this edge comes from.
-- is_my_move: 1 if it was your turn (your choice), 0 if opponent's.
-- wins/losses/draws: game outcomes for games that passed through this edge.
CREATE TABLE IF NOT EXISTS moves (
    id               INTEGER PRIMARY KEY,
    from_position_id INTEGER NOT NULL REFERENCES positions(id),
    to_position_id   INTEGER NOT NULL REFERENCES positions(id),
    uci              TEXT    NOT NULL,
    san              TEXT    NOT NULL,
    repertoire       TEXT    NOT NULL CHECK(repertoire IN ('white', 'black')),
    is_my_move       INTEGER NOT NULL DEFAULT 0,
    frequency        INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    draws            INTEGER NOT NULL DEFAULT 0,
    engine_eval      REAL,
    engine_depth     INTEGER,
    flags            TEXT    NOT NULL DEFAULT '[]',
    notes            TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '[]',
    UNIQUE(from_position_id, uci, repertoire)
);

CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY,
    chess_com_id TEXT UNIQUE,
    pgn          TEXT NOT NULL,
    played_at    TEXT,
    color        TEXT NOT NULL CHECK(color IN ('white', 'black')),
    result       TEXT,
    opponent     TEXT,
    time_control TEXT,
    imported_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Links each game to the opening moves it contributed to.
CREATE TABLE IF NOT EXISTS game_moves (
    id       INTEGER PRIMARY KEY,
    game_id  INTEGER NOT NULL REFERENCES games(id),
    move_id  INTEGER NOT NULL REFERENCES moves(id),
    ply      INTEGER NOT NULL,
    UNIQUE(game_id, ply)
);

CREATE INDEX IF NOT EXISTS idx_moves_from     ON moves(from_position_id, repertoire);
CREATE INDEX IF NOT EXISTS idx_moves_to       ON moves(to_position_id, repertoire);
CREATE INDEX IF NOT EXISTS idx_game_moves_gid ON game_moves(game_id);
CREATE INDEX IF NOT EXISTS idx_games_color    ON games(color);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(db_path: Path = DB_PATH):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with transaction(db_path) as conn:
        conn.executescript(SCHEMA)


def upsert_position(conn: sqlite3.Connection, fen: str) -> int:
    conn.execute("INSERT OR IGNORE INTO positions (fen) VALUES (?)", (fen,))
    row = conn.execute("SELECT id FROM positions WHERE fen = ?", (fen,)).fetchone()
    return row["id"]


def upsert_move(
    conn: sqlite3.Connection,
    from_pos_id: int,
    to_pos_id: int,
    uci: str,
    san: str,
    repertoire: str,
    is_my_move: bool,
    win: int = 0,
    loss: int = 0,
    draw: int = 0,
) -> int:
    conn.execute(
        """
        INSERT INTO moves
            (from_position_id, to_position_id, uci, san, repertoire,
             is_my_move, frequency, wins, losses, draws)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(from_position_id, uci, repertoire) DO UPDATE SET
            frequency      = frequency + 1,
            wins           = wins      + excluded.wins,
            losses         = losses    + excluded.losses,
            draws          = draws     + excluded.draws,
            to_position_id = excluded.to_position_id,
            san            = excluded.san,
            is_my_move     = MAX(is_my_move, excluded.is_my_move)
        """,
        (from_pos_id, to_pos_id, uci, san, repertoire, int(is_my_move), win, loss, draw),
    )
    row = conn.execute(
        "SELECT id FROM moves WHERE from_position_id=? AND uci=? AND repertoire=?",
        (from_pos_id, uci, repertoire),
    ).fetchone()
    return row["id"]


def record_game(
    conn: sqlite3.Connection,
    chess_com_id: str,
    pgn: str,
    played_at: str | None,
    color: str,
    result: str,
    opponent: str,
    time_control: str,
) -> int | None:
    """Insert a game; returns None if already imported (duplicate chess_com_id)."""
    try:
        conn.execute(
            """
            INSERT INTO games (chess_com_id, pgn, played_at, color, result, opponent, time_control)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chess_com_id, pgn, played_at, color, result, opponent, time_control),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.IntegrityError:
        return None


def record_game_move(conn: sqlite3.Connection, game_id: int, move_id: int, ply: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO game_moves (game_id, move_id, ply) VALUES (?, ?, ?)",
        (game_id, move_id, ply),
    )


def get_last_import_date(conn: sqlite3.Connection, color: str) -> str | None:
    """Return the most recent played_at timestamp imported for a given color."""
    row = conn.execute(
        "SELECT MAX(played_at) as last FROM games WHERE color = ?", (color,)
    ).fetchone()
    return row["last"] if row else None
