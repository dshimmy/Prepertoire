"""Stockfish integration for position evaluation and move annotation."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import chess
import chess.engine

# On macOS, tkinter initialises CoreFoundation (via AppKit).  When a background
# thread subsequently forks a subprocess (Stockfish), the child emits a safety
# warning before exec'ing.  Setting this env var before any popen call tells the
# Objective-C runtime in the child to skip the check.
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

from db import DB_PATH, get_connection, transaction

_STOCKFISH_CANDIDATES = [
    Path(__file__).parent.parent / "data" / "stockfish" / "stockfish",
    Path("/usr/local/bin/stockfish"),
    Path("/usr/bin/stockfish"),
    Path("/opt/homebrew/bin/stockfish"),
]

# Centipawn drop thresholds (from mover's perspective, after the move)
_THRESHOLDS = {"blunder": 200, "mistake": 100, "inaccuracy": 50}


def find_stockfish() -> Path | None:
    for p in _STOCKFISH_CANDIDATES:
        if p.exists():
            return p
    found = shutil.which("stockfish")
    return Path(found) if found else None


class StockfishEngine:
    """
    Context manager wrapping a Stockfish subprocess.

    Usage:
        with StockfishEngine(depth=15) as eng:
            score = eng.evaluate(board)
    """

    def __init__(self, path: Path | None = None, depth: int = 15):
        self.path = path or find_stockfish()
        if self.path is None:
            raise FileNotFoundError(
                "Stockfish not found. Place the binary at data/stockfish/stockfish "
                "or install it system-wide (e.g. `brew install stockfish`)."
            )
        self.depth = depth
        self._engine: chess.engine.SimpleEngine | None = None

    def __enter__(self) -> StockfishEngine:
        self._engine = chess.engine.SimpleEngine.popen_uci(str(self.path))
        return self

    def __exit__(self, *_) -> None:
        if self._engine:
            self._engine.quit()
            self._engine = None

    def _require_open(self) -> chess.engine.SimpleEngine:
        if self._engine is None:
            raise RuntimeError("Use StockfishEngine as a context manager.")
        return self._engine

    def evaluate_white(self, board: chess.Board) -> float | None:
        """
        Return centipawns from white's perspective, or None if it's a forced mate.
        Positive = white is better.
        """
        engine = self._require_open()
        info = engine.analyse(board, chess.engine.Limit(depth=self.depth))
        score = info["score"].white()
        if score.is_mate():
            return None
        return float(score.score())  # type: ignore[arg-type]

    def best_move_uci(self, board: chess.Board) -> str:
        engine = self._require_open()
        result = engine.play(board, chess.engine.Limit(depth=self.depth))
        return result.move.uci()

    def top_moves(self, board: chess.Board, n: int = 3) -> list[dict]:
        """Return the top n moves with evaluations (from white's perspective).

        Each dict has keys: 'san', 'uci', 'eval_cp' (float|None), 'eval_str' (str).
        """
        engine = self._require_open()
        info_list = engine.analyse(board, chess.engine.Limit(depth=self.depth), multipv=n)
        if isinstance(info_list, dict):
            info_list = [info_list]
        results = []
        for info in info_list:
            pv = info.get("pv", [])
            if not pv:
                continue
            move = pv[0]
            score = info["score"].white()
            if score.is_mate():
                m = score.mate()
                eval_cp = None
                eval_str = f"M{m:+d}" if m else "M0"
            else:
                cp = float(score.score())  # type: ignore[arg-type]
                eval_cp = cp
                eval_str = f"{cp / 100:+.2f}"
            results.append({
                "uci": move.uci(),
                "san": board.san(move),
                "eval_cp": eval_cp,
                "eval_str": eval_str,
            })
        return results


def _classify_drop(drop_cp: float) -> str | None:
    if drop_cp >= _THRESHOLDS["blunder"]:
        return "blunder"
    if drop_cp >= _THRESHOLDS["mistake"]:
        return "mistake"
    if drop_cp >= _THRESHOLDS["inaccuracy"]:
        return "inaccuracy"
    return None


def annotate_opening(
    repertoire: str,
    depth: int = 15,
    db_path: Path = DB_PATH,
    verbose: bool = True,
) -> int:
    """
    Evaluate all unannotated opening moves and write engine_eval + flags to DB.
    Returns number of moves annotated.
    """
    conn = get_connection(db_path)
    unannotated = conn.execute(
        """
        SELECT m.id, m.uci, m.flags, m.is_my_move,
               pf.fen AS from_fen
        FROM moves m
        JOIN positions pf ON pf.id = m.from_position_id
        WHERE m.repertoire = ? AND m.engine_eval IS NULL
        ORDER BY m.frequency DESC
        """,
        (repertoire,),
    ).fetchall()
    conn.close()

    if not unannotated:
        return 0

    sf_path = find_stockfish()
    if sf_path is None:
        raise FileNotFoundError(
            "Stockfish not found. Cannot annotate. "
            "Place binary at data/stockfish/stockfish or install system-wide."
        )

    count = 0
    with StockfishEngine(sf_path, depth=depth) as eng:
        with transaction(db_path) as conn:
            for row in unannotated:
                try:
                    board = chess.Board(row["from_fen"] + " 0 1")
                except Exception:
                    continue

                move = chess.Move.from_uci(row["uci"])
                if move not in board.legal_moves:
                    continue

                # Evaluate before
                eval_before = eng.evaluate_white(board)
                mover_is_white = board.turn == chess.WHITE
                board.push(move)
                eval_after = eng.evaluate_white(board)

                # Classification: how much did the position worsen for the mover?
                flag = None
                if eval_before is not None and eval_after is not None:
                    # Drop from mover's perspective (positive = bad move)
                    if mover_is_white:
                        drop = eval_before - eval_after
                    else:
                        drop = eval_after - eval_before
                    flag = _classify_drop(drop)

                flags = json.loads(row["flags"] or "[]")
                if flag and flag not in flags:
                    flags.append(flag)

                conn.execute(
                    "UPDATE moves SET engine_eval = ?, engine_depth = ?, flags = ? WHERE id = ?",
                    (eval_after, depth, json.dumps(flags), row["id"]),
                )
                count += 1
                if verbose and count % 20 == 0:
                    print(f"  Annotated {count} moves...", flush=True)

    return count
