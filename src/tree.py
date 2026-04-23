from __future__ import annotations

import json
from dataclasses import dataclass, field

import chess


def normalize_fen(board: chess.Board) -> str:
    """
    Canonical position key for transposition detection.
    Keeps piece placement, side to move, castling rights, and en passant square.
    Drops the halfmove clock and fullmove number — those vary across transpositions
    but don't change which position we're in for opening purposes.
    """
    parts = board.fen().split()
    return " ".join(parts[:4])


@dataclass
class MoveEdge:
    uci: str
    san: str
    frequency: int
    wins: int
    losses: int
    draws: int
    is_my_move: bool
    engine_eval: float | None
    flags: list[str]
    notes: str
    tags: list[str]
    to_fen: str

    @property
    def total_games(self) -> int:
        return self.wins + self.losses + self.draws

    @property
    def score_pct(self) -> float | None:
        """Win + half-draw percentage (standard chess scoring)."""
        if self.total_games == 0:
            return None
        return (self.wins + 0.5 * self.draws) / self.total_games

    @property
    def win_rate(self) -> float | None:
        if self.total_games == 0:
            return None
        return self.wins / self.total_games

    def summary(self) -> str:
        score = f"{self.score_pct:.0%}" if self.score_pct is not None else "n/a"
        flag_str = f" [{', '.join(self.flags)}]" if self.flags else ""
        return f"{self.san:8s}  freq={self.frequency:4d}  score={score}{flag_str}"


@dataclass
class PositionNode:
    fen: str
    moves: list[MoveEdge] = field(default_factory=list)

    @property
    def my_moves(self) -> list[MoveEdge]:
        return [m for m in self.moves if m.is_my_move]

    @property
    def opponent_moves(self) -> list[MoveEdge]:
        return [m for m in self.moves if not m.is_my_move]

    def best_move(self) -> MoveEdge | None:
        """Most frequently played move of yours from this position."""
        if not self.my_moves:
            return None
        return max(self.my_moves, key=lambda m: m.frequency)

    def has_coverage_gap(self) -> bool:
        """
        True if this is your turn but there's no clear preferred move —
        either no moves at all, or the top two are within 50% of each other.
        """
        my = sorted(self.my_moves, key=lambda m: m.frequency, reverse=True)
        if not my:
            return True
        if len(my) >= 2 and my[0].frequency <= my[1].frequency * 1.5:
            return True
        return False


def edges_from_db_rows(rows) -> list[MoveEdge]:
    return [
        MoveEdge(
            uci=r["uci"],
            san=r["san"],
            frequency=r["frequency"],
            wins=r["wins"],
            losses=r["losses"],
            draws=r["draws"],
            is_my_move=bool(r["is_my_move"]),
            engine_eval=r["engine_eval"],
            flags=json.loads(r["flags"] or "[]"),
            notes=r["notes"] or "",
            tags=json.loads(r["tags"] or "[]"),
            to_fen=r["to_fen"],
        )
        for r in rows
    ]
