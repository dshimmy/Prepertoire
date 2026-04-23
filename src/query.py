"""Lookup positions and moves from the repertoire database."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from db import DB_PATH, get_connection
from tree import MoveEdge, PositionNode, edges_from_db_rows, normalize_fen

_MOVES_SQL = """
    SELECT m.uci, m.san, m.frequency, m.wins, m.losses, m.draws,
           m.is_my_move, m.engine_eval, m.flags, m.notes, m.tags,
           p.fen AS to_fen
    FROM moves m
    JOIN positions p ON p.id = m.to_position_id
    WHERE m.from_position_id = ? AND m.repertoire = ?
    ORDER BY m.frequency DESC
"""


def get_node(conn: sqlite3.Connection, fen: str, repertoire: str) -> PositionNode | None:
    pos_row = conn.execute("SELECT id FROM positions WHERE fen = ?", (fen,)).fetchone()
    if pos_row is None:
        return None
    rows = conn.execute(_MOVES_SQL, (pos_row["id"], repertoire)).fetchall()
    return PositionNode(fen=fen, moves=edges_from_db_rows(rows))


def _apply_moves(moves: list[str]) -> chess.Board | None:
    board = chess.Board()
    for m in moves:
        try:
            move = board.parse_uci(m)
        except ValueError:
            try:
                move = board.parse_san(m)
            except ValueError:
                return None
        board.push(move)
    return board


def lookup(
    moves: list[str],
    repertoire: str,
    db_path: Path = DB_PATH,
) -> PositionNode | None:
    """Follow a move sequence from the start and return the PositionNode there."""
    board = _apply_moves(moves)
    if board is None:
        return None
    conn = get_connection(db_path)
    try:
        return get_node(conn, normalize_fen(board), repertoire)
    finally:
        conn.close()


def lookup_from_pgn(
    pgn_snippet: str,
    repertoire: str,
    db_path: Path = DB_PATH,
) -> PositionNode | None:
    """Parse a PGN snippet and return the node at the final position."""
    game = chess.pgn.read_game(io.StringIO(pgn_snippet))
    if game is None:
        return None
    board = game.board()
    for move in game.mainline_moves():
        board.push(move)
    conn = get_connection(db_path)
    try:
        return get_node(conn, normalize_fen(board), repertoire)
    finally:
        conn.close()


def get_tree(
    moves: list[str],
    repertoire: str,
    depth: int = 3,
    db_path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return a nested dict of the opening tree from the given position, up to `depth` levels."""
    board = _apply_moves(moves)
    if board is None:
        return None
    fen = normalize_fen(board)
    conn = get_connection(db_path)
    try:
        return _build_tree(conn, fen, repertoire, depth, visited=set())
    finally:
        conn.close()


def _build_tree(
    conn: sqlite3.Connection,
    fen: str,
    repertoire: str,
    depth: int,
    visited: set[str],
) -> dict[str, Any] | None:
    node = get_node(conn, fen, repertoire)
    if node is None:
        return None
    result: dict[str, Any] = {"fen": fen, "moves": []}
    for edge in node.moves:
        entry: dict[str, Any] = {
            "uci": edge.uci,
            "san": edge.san,
            "frequency": edge.frequency,
            "is_my_move": edge.is_my_move,
            "score_pct": edge.score_pct,
            "engine_eval": edge.engine_eval,
            "flags": edge.flags,
            "notes": edge.notes,
            "children": [],
        }
        if depth > 1 and edge.to_fen not in visited:
            child = _build_tree(conn, edge.to_fen, repertoire, depth - 1, visited | {fen})
            if child:
                entry["children"] = child["moves"]
        result["moves"].append(entry)
    return result


def find_coverage_gaps(
    repertoire: str,
    db_path: Path = DB_PATH,
) -> list[tuple[str, PositionNode]]:
    """Return (fen, node) pairs where it's your turn but no clear preferred move exists."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT p.fen
            FROM positions p
            JOIN moves m ON m.from_position_id = p.id
            WHERE m.repertoire = ? AND m.is_my_move = 1
            """,
            (repertoire,),
        ).fetchall()
        gaps = []
        for row in rows:
            node = get_node(conn, row["fen"], repertoire)
            if node and node.has_coverage_gap():
                gaps.append((row["fen"], node))
        return gaps
    finally:
        conn.close()


def format_node(node: PositionNode, title: str = "") -> str:
    """Human-readable summary of a PositionNode."""
    lines = []
    if title:
        lines.append(title)
    if not node.moves:
        lines.append("  (no moves in repertoire from this position)")
        return "\n".join(lines)

    my = node.my_moves
    opp = node.opponent_moves

    if my:
        # Single correct move — most frequent
        best = node.best_move()
        lines.append("Your move:")
        lines.append(f"  {best.summary()}")
        if best.notes:
            lines.append(f"    Note: {best.notes}")
    if opp:
        lines.append("Opponent moves seen (top 5):")
        for m in sorted(opp, key=lambda x: x.frequency, reverse=True)[:5]:
            lines.append(f"  {m.summary()}")
    return "\n".join(lines)


def get_stats(repertoire: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    """Return aggregate statistics for a repertoire."""
    conn = get_connection(db_path)
    try:
        total_games = conn.execute(
            "SELECT COUNT(*) FROM games WHERE color = ?", (repertoire,)
        ).fetchone()[0]
        total_positions = conn.execute(
            "SELECT COUNT(DISTINCT from_position_id) FROM moves WHERE repertoire = ?",
            (repertoire,),
        ).fetchone()[0]
        total_moves = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE repertoire = ?", (repertoire,)
        ).fetchone()[0]
        my_moves = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE repertoire = ? AND is_my_move = 1", (repertoire,)
        ).fetchone()[0]
        gap_count = len(find_coverage_gaps(repertoire, db_path))
        return {
            "total_games": total_games,
            "total_positions": total_positions,
            "total_moves": total_moves,
            "my_moves": my_moves,
            "coverage_gaps": gap_count,
        }
    finally:
        conn.close()
