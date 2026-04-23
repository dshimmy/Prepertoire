"""Incremental import and manual repertoire edits."""

from __future__ import annotations

import io
import json
from pathlib import Path

import chess
import chess.pgn

from db import DB_PATH, get_connection, init_db, transaction, upsert_move, upsert_position
from importer import ImportStats, import_games
from tree import normalize_fen


def incremental_import(username: str, db_path: Path = DB_PATH) -> ImportStats:
    """Import only games not yet in the DB (auto-detects last import date)."""
    return import_games(username, since=None, db_path=db_path)


def add_manual_line(
    moves: list[str],
    repertoire: str,
    notes: str = "",
    tags: list[str] | None = None,
    db_path: Path = DB_PATH,
) -> int:
    """
    Add a line (list of UCI or SAN moves) to the repertoire.
    Returns the number of moves inserted or updated.
    """
    init_db(db_path)

    # Pre-parse all moves before touching the DB
    board = chess.Board()
    parsed: list[tuple[str, str, bool]] = []  # (uci, san, is_white_to_move)
    for m in moves:
        try:
            move = board.parse_uci(m)
        except ValueError:
            move = board.parse_san(m)
        parsed.append((move.uci(), board.san(move), board.turn == chess.WHITE))
        board.push(move)

    count = 0
    with transaction(db_path) as conn:
        board = chess.Board()
        prev_id = upsert_position(conn, normalize_fen(board))

        for uci, san, white_to_move in parsed:
            move = chess.Move.from_uci(uci)
            board.push(move)
            next_id = upsert_position(conn, normalize_fen(board))
            is_my_move = white_to_move == (repertoire == "white")
            move_id = upsert_move(conn, prev_id, next_id, uci, san, repertoire, is_my_move)

            if notes or tags:
                existing = conn.execute(
                    "SELECT notes, tags FROM moves WHERE id = ?", (move_id,)
                ).fetchone()
                new_notes = notes or existing["notes"]
                existing_tags: list[str] = json.loads(existing["tags"] or "[]")
                new_tags = list(dict.fromkeys(existing_tags + (tags or [])))
                conn.execute(
                    "UPDATE moves SET notes = ?, tags = ? WHERE id = ?",
                    (new_notes, json.dumps(new_tags), move_id),
                )

            prev_id = next_id
            count += 1

    return count


def add_manual_pgn(
    pgn_str: str,
    repertoire: str,
    notes: str = "",
    db_path: Path = DB_PATH,
) -> int:
    """Parse a PGN string and add all mainline moves to the repertoire."""
    game = chess.pgn.read_game(io.StringIO(pgn_str))
    if game is None:
        raise ValueError("Could not parse PGN.")
    board = game.board()
    uci_moves = []
    for move in game.mainline_moves():
        uci_moves.append(move.uci())
        board.push(move)
    return add_manual_line(uci_moves, repertoire, notes=notes, db_path=db_path)


def update_annotation(
    moves: list[str],
    repertoire: str,
    notes: str | None = None,
    tags: list[str] | None = None,
    flags: list[str] | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    """
    Update notes/tags/flags on the final move of a sequence.
    Tags and flags are additive (merged with existing). Notes replace existing.
    Returns True if the move was found.
    """
    if not moves:
        return False

    board = chess.Board()
    for m in moves[:-1]:
        try:
            move = board.parse_uci(m)
        except ValueError:
            move = board.parse_san(m)
        board.push(move)

    from_fen = normalize_fen(board)
    try:
        last_move = board.parse_uci(moves[-1])
    except ValueError:
        last_move = board.parse_san(moves[-1])
    uci = last_move.uci()

    with transaction(db_path) as conn:
        pos = conn.execute("SELECT id FROM positions WHERE fen = ?", (from_fen,)).fetchone()
        if pos is None:
            return False
        row = conn.execute(
            "SELECT id, notes, tags, flags FROM moves "
            "WHERE from_position_id = ? AND uci = ? AND repertoire = ?",
            (pos["id"], uci, repertoire),
        ).fetchone()
        if row is None:
            return False

        new_notes = notes if notes is not None else row["notes"]
        existing_tags: list[str] = json.loads(row["tags"] or "[]")
        new_tags = list(dict.fromkeys(existing_tags + (tags or [])))
        existing_flags: list[str] = json.loads(row["flags"] or "[]")
        new_flags = list(dict.fromkeys(existing_flags + (flags or [])))

        conn.execute(
            "UPDATE moves SET notes = ?, tags = ?, flags = ? WHERE id = ?",
            (new_notes, json.dumps(new_tags), json.dumps(new_flags), row["id"]),
        )
    return True


def set_as_book_move(
    moves: list[str],
    repertoire: str,
    db_path: Path = DB_PATH,
) -> None:
    """
    Add a line and ensure its final move has the highest frequency from that
    position, making it the repertoire's preferred (book) move.
    """
    add_manual_line(moves, repertoire, db_path=db_path)

    board = chess.Board()
    for m in moves[:-1]:
        try:
            move = board.parse_uci(m)
        except ValueError:
            move = board.parse_san(m)
        board.push(move)
    from_fen = normalize_fen(board)
    try:
        last_move = board.parse_uci(moves[-1])
    except ValueError:
        last_move = board.parse_san(moves[-1])
    uci = last_move.uci()

    with transaction(db_path) as conn:
        row = conn.execute(
            """SELECT COALESCE(MAX(m.frequency), 0) AS max_freq
               FROM moves m JOIN positions p ON p.id = m.from_position_id
               WHERE p.fen = ? AND m.repertoire = ? AND m.is_my_move = 1""",
            (from_fen, repertoire),
        ).fetchone()
        conn.execute(
            """UPDATE moves SET frequency = ?
               WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                 AND uci = ? AND repertoire = ?""",
            (row["max_freq"] + 1, from_fen, uci, repertoire),
        )


def flag_deviation(
    game_pgn: str,
    deviation_ply: int,
    intended_response: str,
    repertoire: str,
    db_path: Path = DB_PATH,
) -> bool:
    """
    Record your intended response after an opponent deviation.

    deviation_ply: 0-based ply index of the opponent's unexpected move.
    intended_response: the move you wanted to play (UCI or SAN), applied after deviation_ply.
    """
    game = chess.pgn.read_game(io.StringIO(game_pgn))
    if game is None:
        return False

    board = game.board()
    game_moves = list(game.mainline_moves())

    for move in game_moves[: deviation_ply + 1]:
        board.push(move)

    try:
        resp = board.parse_uci(intended_response)
    except ValueError:
        resp = board.parse_san(intended_response)

    line = [m.uci() for m in game_moves[: deviation_ply + 1]] + [resp.uci()]
    add_manual_line(
        line,
        repertoire,
        notes="flagged deviation — intended response",
        db_path=db_path,
    )
    return True


_TARGET_PLY = 20  # 10 full moves


def build_continuations_from_games(
    cur_fen: str,
    history_ucis: list[str],
    repertoire: str,
    db_path: Path = DB_PATH,
) -> int:
    """
    Scan every stored game that passed through cur_fen and import the moves
    that followed it into the repertoire tree, up to a total line length of
    10 moves (20 plies) from the starting position.

    Each matching game contributes +1 frequency to the continuation moves it
    played, so the most common responses accumulate the highest frequency —
    just like the original import.

    Returns the number of source games that contributed new continuations.
    """
    # How many more plies can we add before hitting the 10-move cap?
    max_extra_ply = _TARGET_PLY - len(history_ucis)
    if max_extra_ply <= 0:
        return 0  # already at or past 10 moves; nothing to add

    conn = get_connection(db_path)
    try:
        pos_row = conn.execute(
            "SELECT id FROM positions WHERE fen = ?", (cur_fen,)
        ).fetchone()
        if pos_row is None:
            return 0
        pos_id = pos_row["id"]

        # Find games where a move arrived at cur_fen (recorded within max_ply range).
        # gm.ply is 0-based: the move at ply P leads to cur_fen, so continuation
        # starts at index P+1 in the game's move list.
        game_rows = conn.execute(
            """
            SELECT DISTINCT g.id, g.pgn, gm.ply
            FROM games g
            JOIN game_moves gm ON gm.game_id = g.id
            JOIN moves m       ON m.id = gm.move_id
            WHERE m.to_position_id = ? AND g.color = ?
            ORDER BY g.id
            """,
            (pos_id, repertoire),
        ).fetchall()
    finally:
        conn.close()

    if not game_rows:
        return 0

    games_used = 0
    for game_row in game_rows:
        try:
            game = chess.pgn.read_game(io.StringIO(game_row["pgn"]))
        except Exception:
            continue
        if game is None:
            continue

        all_moves = list(game.mainline_moves())
        cont_start = game_row["ply"] + 1          # index of first continuation move
        cont_moves = all_moves[cont_start: cont_start + max_extra_ply]
        if not cont_moves:
            continue

        # Verify the board really is at cur_fen at cont_start
        board = game.board()
        for mv in all_moves[:cont_start]:
            board.push(mv)
        if normalize_fen(board) != cur_fen:
            continue

        full_line = history_ucis + [m.uci() for m in cont_moves]
        try:
            add_manual_line(full_line, repertoire, db_path=db_path)
            games_used += 1
        except Exception:
            continue

    return games_used
