"""Fetch games from Chess.com API and import them into the repertoire database."""

from __future__ import annotations

import calendar
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import chess
import chess.pgn
import requests

from db import (
    DB_PATH,
    get_connection,
    get_last_import_date,
    init_db,
    record_game,
    record_game_move,
    upsert_move,
    upsert_position,
)
from tree import normalize_fen

CHESS_COM_BASE = "https://api.chess.com/pub/player"
DEFAULT_MAX_PLY = 20


@dataclass
class ImportStats:
    games_found: int = 0
    games_imported: int = 0
    games_skipped: int = 0
    moves_added: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"Found {self.games_found} games:",
            f"  {self.games_imported} imported",
            f"  {self.games_skipped} already in DB",
            f"  {self.moves_added} opening moves recorded",
        ]
        if self.errors:
            parts.append(f"  {len(self.errors)} errors")
        return "\n".join(parts)


def _api_headers() -> dict[str, str]:
    return {"User-Agent": "chess-repertoire-tool/1.0 (personal use)"}


def fetch_archive_urls(username: str) -> list[str]:
    url = f"{CHESS_COM_BASE}/{username}/games/archives"
    resp = requests.get(url, headers=_api_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("archives", [])


def fetch_month_games(archive_url: str) -> list[dict]:
    resp = requests.get(archive_url, headers=_api_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("games", [])


def games_since(username: str, since_iso: str | None = None) -> Iterator[dict]:
    """Yield game dicts from Chess.com (newest first), skipping games at or before since_iso."""
    archives = fetch_archive_urls(username)
    since_dt: datetime | None = None
    if since_iso:
        since_dt = datetime.fromisoformat(since_iso)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

    for url in reversed(archives):
        if since_dt:
            parts = url.rstrip("/").split("/")
            try:
                arch_year, arch_month = int(parts[-2]), int(parts[-1])
                last_day = calendar.monthrange(arch_year, arch_month)[1]
                arch_end = datetime(arch_year, arch_month, last_day, 23, 59, 59, tzinfo=timezone.utc)
                if arch_end <= since_dt:
                    break  # this archive and all older ones are fully covered
            except (ValueError, IndexError):
                pass

        for g in fetch_month_games(url):
            end_ts = g.get("end_time")
            if since_dt and end_ts:
                played_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
                if played_dt <= since_dt:
                    continue
            yield g


def _parse_result(pgn_result: str, color: str) -> tuple[int, int, int]:
    """Return (win, loss, draw) counts for the given color."""
    if pgn_result == "1/2-1/2":
        return 0, 0, 1
    if (pgn_result == "1-0") == (color == "white"):
        return 1, 0, 0
    return 0, 1, 0


def import_games(
    username: str,
    since: str | None = None,
    max_ply: int = DEFAULT_MAX_PLY,
    db_path: Path = DB_PATH,
    verbose: bool = True,
) -> ImportStats:
    """
    Import games from Chess.com into the repertoire DB.

    If since is None, auto-detects the most recent import date from the DB
    so only new games are fetched on subsequent runs.
    """
    init_db(db_path)
    stats = ImportStats()
    conn = get_connection(db_path)
    try:
        if since is None:
            dates = [
                d
                for d in [get_last_import_date(conn, "white"), get_last_import_date(conn, "black")]
                if d
            ]
            if dates:
                since = min(dates)
            else:
                # First-time import: default to one year back so old openings are excluded
                one_year_ago = datetime.now(tz=timezone.utc).replace(year=datetime.now().year - 1)
                since = one_year_ago.isoformat()

        for game_dict in games_since(username, since):
            stats.games_found += 1

            white_user = game_dict.get("white", {}).get("username", "").lower()
            black_user = game_dict.get("black", {}).get("username", "").lower()
            u = username.lower()
            if white_user == u:
                color = "white"
            elif black_user == u:
                color = "black"
            else:
                continue

            pgn_str = game_dict.get("pgn", "")
            if not pgn_str:
                continue

            try:
                game = chess.pgn.read_game(io.StringIO(pgn_str))
            except Exception as exc:
                stats.errors.append(str(exc))
                continue
            if game is None:
                continue

            headers = game.headers
            end_ts = game_dict.get("end_time")
            game_url = game_dict.get("url", "")
            chess_com_id = game_url.split("/")[-1] or str(end_ts or "")
            played_at = (
                datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat() if end_ts else None
            )
            result = headers.get("Result", "*")
            opponent = headers.get("Black" if color == "white" else "White", "?")
            time_control = headers.get("TimeControl", "?")

            game_id = record_game(
                conn, chess_com_id, pgn_str, played_at, color, result, opponent, time_control
            )
            if game_id is None:
                stats.games_skipped += 1
                continue

            win, loss, draw = _parse_result(result, color)
            board = game.board()
            ply = 0
            prev_pos_id = upsert_position(conn, normalize_fen(board))

            for move in game.mainline_moves():
                if ply >= max_ply:
                    break
                san = board.san(move)
                uci = move.uci()
                is_my_move = (board.turn == chess.WHITE) == (color == "white")
                board.push(move)
                fen = normalize_fen(board)
                next_pos_id = upsert_position(conn, fen)
                move_id = upsert_move(
                    conn, prev_pos_id, next_pos_id, uci, san, color, is_my_move, win, loss, draw
                )
                record_game_move(conn, game_id, move_id, ply)
                prev_pos_id = next_pos_id
                ply += 1
                stats.moves_added += 1

            stats.games_imported += 1
            if verbose and stats.games_imported % 50 == 0:
                print(f"  Imported {stats.games_imported} games...", flush=True)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return stats
