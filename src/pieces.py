"""Load and cache Lichess SVG piece sets as tkinter-ready images."""

from __future__ import annotations

import io
from pathlib import Path

import cairosvg
import chess
from PIL import Image, ImageTk

SETS_DIR = Path(__file__).parent.parent / "data" / "pieces"

_FILENAMES: dict[tuple[int, int], str] = {
    (chess.KING,   chess.WHITE): "wK",
    (chess.QUEEN,  chess.WHITE): "wQ",
    (chess.ROOK,   chess.WHITE): "wR",
    (chess.BISHOP, chess.WHITE): "wB",
    (chess.KNIGHT, chess.WHITE): "wN",
    (chess.PAWN,   chess.WHITE): "wP",
    (chess.KING,   chess.BLACK): "bK",
    (chess.QUEEN,  chess.BLACK): "bQ",
    (chess.ROOK,   chess.BLACK): "bR",
    (chess.BISHOP, chess.BLACK): "bB",
    (chess.KNIGHT, chess.BLACK): "bN",
    (chess.PAWN,   chess.BLACK): "bP",
}


def load_set(name: str, size: int) -> dict[tuple[int, int], ImageTk.PhotoImage]:
    """
    Return a dict mapping (piece_type, color) → PhotoImage for the named set.
    Images are rendered at `size` × `size` pixels.
    """
    set_dir = SETS_DIR / name
    images: dict[tuple[int, int], ImageTk.PhotoImage] = {}
    for key, fname in _FILENAMES.items():
        svg_path = set_dir / f"{fname}.svg"
        if not svg_path.exists():
            raise FileNotFoundError(f"Missing piece file: {svg_path}")
        png_bytes = cairosvg.svg2png(
            bytestring=svg_path.read_bytes(),
            output_width=size,
            output_height=size,
        )
        pil_img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        images[key] = ImageTk.PhotoImage(pil_img)
    return images


def available_sets() -> list[str]:
    """Return names of piece sets present in data/pieces/."""
    if not SETS_DIR.exists():
        return []
    return sorted(d.name for d in SETS_DIR.iterdir() if d.is_dir())
