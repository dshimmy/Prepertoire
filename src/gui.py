"""tkinter-based interactive repertoire review board."""

from __future__ import annotations

import json
import random
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from datetime import date
from pathlib import Path
from typing import Any

import chess

from db import DB_PATH, get_connection, transaction, upsert_position, upsert_move
from drill import init_drill_db, record_result, toggle_drill_stop, auto_label_alternatives
from engine import StockfishEngine, find_stockfish
from openings import lookup_opening, get_openings_split, compute_filter_fens
from pieces import load_set
from query import get_node, find_coverage_gaps
from tree import MoveEdge, PositionNode, normalize_fen
from updater import add_manual_line, set_as_book_move, update_annotation

SQUARE_PX = 72
BOARD_PX = SQUARE_PX * 8
LIGHT_SQ = "#F0D9B5"
DARK_SQ = "#B58863"
HIGHLIGHT_SQ = "#7FC97F"
LAST_MOVE_SQ = "#CDD16E"
GAP_COLOR = "#FF6B6B"

PIECE_SET = "cburnett"


class RepertoireGUI:
    def __init__(self, root: tk.Tk, repertoire: str, db_path: Path = DB_PATH) -> None:
        self.root = root
        self.repertoire = repertoire
        self.db_path = db_path
        self.board = chess.Board()
        self.history: list[chess.Move] = []
        self.current_node: PositionNode | None = None
        self.last_edge: MoveEdge | None = None  # move that led to current position
        self._selected_sq: chess.Square | None = None
        self._last_move_sqs: tuple[chess.Square, chess.Square] | None = None
        self._forward_stack: list[chess.Move] = []
        self._drag_from_sq: chess.Square | None = None
        self._drag_img_id: int | None = None
        # Drill state
        self._drill_mode: bool = False
        self._drill_correct: int = 0
        self._drill_total: int = 0
        self._drill_awaiting_correction: bool = False
        self._drill_book_move: chess.Move | None = None
        self._drill_book_move_id: int | None = None
        self._drill_book_move_ef: float = 2.5
        self._drill_book_move_interval: int = 1
        self._drill_hint_sqs: set[chess.Square] = set()
        self._drill_game_number: int = 0       # incremented each new game; odd=white, even=black
        self._original_repertoire: str = repertoire  # restored on drill exit
        self._drill_prep_status: str | None = None  # 'green' | 'yellow' | 'red' | 'blue' | None
        self._drill_main_vs_alt: bool = False  # True when player played main line against an alt book move
        # Engine analysis state
        self._analysing: bool = False
        # Drill opening filter: repertoire -> set of opening names; FENs computed on apply
        self._drill_filter: dict[str, set[str]] = {}
        self._drill_filter_fens: dict[str, set[str]] = {}
        # Cached result of get_openings_split for both colors; refreshed in background
        self._openings_split_cache: dict[str, tuple[list, list]] | None = None

        # Load piece images — must be kept alive on self to prevent GC
        self._piece_images = load_set(PIECE_SET, SQUARE_PX)

        root.title(f"Repertoire Review — {repertoire.capitalize()}")
        root.resizable(False, False)
        root.geometry(f"{BOARD_PX + 368}x{BOARD_PX + 144}")
        self._build_ui()
        self._sync_color_button()
        init_drill_db(self.db_path)  # ensures prep_status column exists before any labeling
        auto_label_alternatives(self.db_path)
        self._refresh_openings_split_cache()
        self._load_position()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        left = tk.Frame(self.root, bg="#2b2b2b")
        left.pack(side=tk.LEFT)

        # Board canvas
        self.canvas = tk.Canvas(left, width=BOARD_PX, height=BOARD_PX, highlightthickness=0)
        self.canvas.pack(padx=8, pady=8)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_release)
        self.root.bind("<Left>", lambda _: self._back())
        self.root.bind("<Right>", lambda _: self._go_forward())

        # Nav bar under board
        nav = tk.Frame(left, bg="#2b2b2b")
        nav.pack(fill=tk.X, padx=8, pady=(0, 8))
        for text, cmd in [("⏮ Start", self._go_start), ("← Back", self._back)]:
            tk.Button(nav, text=text, command=cmd, width=10).pack(side=tk.LEFT, padx=2)
        drill_lbl = tk.Label(nav, text="Drill", bg="#3a7abf", fg="white",
                             font=("Arial", 9), padx=10, pady=3, cursor="hand2")
        drill_lbl.pack(side=tk.RIGHT, padx=2)
        drill_lbl.bind("<Button-1>", lambda _: self._start_drill())
        self._analyse_lbl = tk.Label(nav, text="Analyse", bg="#5a4a8a", fg="white",
                                     font=("Arial", 9), padx=10, pady=3, cursor="hand2")
        self._analyse_lbl.pack(side=tk.RIGHT, padx=2)
        self._analyse_lbl.bind("<Button-1>", lambda _: self._analyse_position())
        self._color_lbl = tk.Label(nav, text="", bg="#7a5a2a", fg="white",
                                   font=("Arial", 9), padx=10, pady=3, cursor="hand2")
        self._color_lbl.pack(side=tk.RIGHT, padx=2)
        self._color_lbl.bind("<Button-1>", lambda _: self._toggle_repertoire_color())

        # Right panel — hosts either the review frame or the drill frame
        right = tk.Frame(self.root, width=320)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        right.pack_propagate(False)

        # ── Engine panel (always visible, pinned to bottom) ──────────────────
        eng_outer = tk.Frame(right)
        eng_outer.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(eng_outer).pack(fill="x")
        eng_hdr = tk.Frame(eng_outer)
        eng_hdr.pack(fill=tk.X, padx=4, pady=(3, 0))
        tk.Label(eng_hdr, text="Engine:", font=("Arial", 9, "bold")).pack(side=tk.LEFT)
        self._engine_pos_var = tk.StringVar(value="")
        tk.Label(eng_hdr, textvariable=self._engine_pos_var,
                 font=("Courier", 9), fg="#5a4a8a").pack(side=tk.LEFT, padx=6)
        self._engine_move_vars: list[tk.StringVar] = []
        self._engine_move_lbls: list[tk.Label] = []
        for _ in range(3):
            var = tk.StringVar(value="")
            lbl = tk.Label(eng_outer, textvariable=var,
                           font=("Courier", 9), fg="#333", anchor="w")
            lbl.pack(fill=tk.X, padx=8)
            self._engine_move_vars.append(var)
            self._engine_move_lbls.append(lbl)

        # ── Review frame ────────────────────────────────────────────────────
        self._review_frame = tk.Frame(right)
        self._review_frame.pack(fill=tk.BOTH, expand=True)
        rv = self._review_frame

        self.moves_label = tk.Label(rv, text="Repertoire moves:", font=("Arial", 9, "bold"))
        self.moves_label.pack(anchor="w")
        list_frame = tk.Frame(rv)
        list_frame.pack(fill=tk.X)
        sb = tk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.moves_lb = tk.Listbox(
            list_frame, height=6, font=("Courier", 10),
            selectbackground="#4a90d9", yscrollcommand=sb.set
        )
        sb.config(command=self.moves_lb.yview)
        self.moves_lb.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.moves_lb.bind("<<ListboxSelect>>", self._on_move_select)
        self.moves_lb.bind("<Double-Button-1>", self._on_move_navigate)
        self.moves_lb.bind("<Return>", self._on_move_navigate)
        tk.Label(rv, text="Double-click or Enter to navigate →", font=("Arial", 8), fg="#888").pack(anchor="w")

        ttk.Separator(rv).pack(fill="x", pady=4)

        tk.Label(rv, text="Prep status (last move):", font=("Arial", 9, "bold")).pack(anchor="w")
        prep_row = tk.Frame(rv)
        prep_row.pack(fill=tk.X, pady=(2, 4))
        self._review_prep_btns: dict[str, tk.Label] = {}
        for status, text, active_bg in [
            ("green",  "● Memorized",  "#2a6e2a"),
            ("yellow", "● In prep",    "#b07a10"),
            ("red",    "● Off-book",   "#8b0000"),
            ("blue",   "● Alternative","#1a4a8a"),
        ]:
            btn = tk.Label(prep_row, text=text, bg="#cccccc", fg="#444444",
                           font=("Arial", 8, "bold"), padx=6, pady=3, cursor="hand2")
            btn.pack(side=tk.LEFT, padx=2)
            btn._active_bg = active_bg  # type: ignore[attr-defined]
            btn.bind("<Button-1>", lambda _, s=status: self._review_set_prep(s))
            self._review_prep_btns[status] = btn
        self._review_prep_status: str | None = None

        tk.Label(rv, text="Move history:", font=("Arial", 9, "bold")).pack(anchor="w", pady=(4, 0))
        rv_notation_frame = tk.Frame(rv)
        rv_notation_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        rv_notation_sb = tk.Scrollbar(rv_notation_frame, orient=tk.VERTICAL)
        self._review_notation_txt = tk.Text(
            rv_notation_frame,
            height=8,
            font=("Courier", 11),
            wrap=tk.NONE,
            state=tk.DISABLED,
            fg="black",
            bg="#ffffff",
            relief=tk.FLAT,
            cursor="arrow",
            yscrollcommand=rv_notation_sb.set,
        )
        self._review_notation_txt.tag_configure("row_even", background="#ffffff", foreground="black")
        self._review_notation_txt.tag_configure("row_odd",  background="#ebebeb", foreground="black")
        self._review_notation_txt.tag_configure("prep_green",  foreground="#1a6e1a")
        self._review_notation_txt.tag_configure("prep_yellow", foreground="#7a5000")
        self._review_notation_txt.tag_configure("prep_red",    foreground="#8b0000")
        self._review_notation_txt.tag_configure("prep_blue",   foreground="#1a4a8a")
        for _t in ("prep_green", "prep_yellow", "prep_red", "prep_blue"):
            self._review_notation_txt.tag_raise(_t)
        rv_notation_sb.config(command=self._review_notation_txt.yview)
        self._review_notation_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rv_notation_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._add_line_lbl = tk.Label(rv, text="Add current line to book", bg="#1a5276", fg="white",
                                      font=("Arial", 9, "bold"), padx=4, pady=5, cursor="hand2")
        self._add_line_lbl.pack(fill=tk.X, pady=(4, 0))
        self._add_line_lbl.bind("<Button-1>", lambda _: self._add_line_to_book())

        ttk.Separator(rv).pack(fill="x", pady=4)

        tk.Label(rv, text="Notes for selected move:", font=("Arial", 9, "bold")).pack(anchor="w")
        self.notes_txt = tk.Text(rv, height=3, width=38, font=("Arial", 9), wrap=tk.WORD)
        self.notes_txt.pack(fill=tk.X)
        tk.Label(rv, text="Tags (comma-separated):", font=("Arial", 9, "bold")).pack(anchor="w", pady=(4, 0))
        self.tags_var = tk.StringVar()
        tk.Entry(rv, textvariable=self.tags_var, font=("Arial", 9)).pack(fill=tk.X)
        save_lbl = tk.Label(rv, text="Save annotation", bg="#4CAF50", fg="white",
                            font=("Arial", 9, "bold"), padx=4, pady=5, cursor="hand2")
        save_lbl.pack(fill=tk.X, pady=4)
        save_lbl.bind("<Button-1>", lambda _: self._save_annotation())

        ttk.Separator(rv).pack(fill="x", pady=4)

        self.gap_var = tk.StringVar()
        self.gap_lbl = tk.Label(rv, textvariable=self.gap_var, fg=GAP_COLOR,
                                font=("Arial", 9, "bold"), wraplength=300, justify="left")
        self.gap_lbl.pack(anchor="w")

        self.eval_var = tk.StringVar(value="")
        tk.Label(rv, textvariable=self.eval_var, font=("Courier", 9), fg="#555").pack(anchor="w")
        self.status_var = tk.StringVar()
        tk.Label(rv, textvariable=self.status_var, font=("Arial", 8), fg="#888").pack(anchor="w", pady=4)

        # ── Drill frame (hidden until drill starts) ──────────────────────────
        self._drill_frame = tk.Frame(right)
        dr = self._drill_frame

        self._drill_header_var = tk.StringVar(value="Drill Mode")
        tk.Label(dr, textvariable=self._drill_header_var,
                 font=("Arial", 13, "bold"), fg="#3a7abf").pack(pady=(8, 4))

        self._drill_progress_var = tk.StringVar(value="")
        tk.Label(dr, textvariable=self._drill_progress_var,
                 font=("Arial", 10), fg="#555").pack()

        ttk.Separator(dr).pack(fill="x", pady=8)

        self._drill_feedback_var = tk.StringVar(value="Drag a piece to make your move.")
        self._drill_feedback_lbl = tk.Label(
            dr, textvariable=self._drill_feedback_var,
            font=("Arial", 12, "bold"), wraplength=280, justify="center",
        )
        self._drill_feedback_lbl.pack(pady=8)

        # Label frame — shown after a correct move on an unlabeled position
        self._drill_label_frame = tk.Frame(dr)
        lf_row = tk.Frame(self._drill_label_frame)
        lf_row.pack(fill=tk.X, padx=4, pady=2)
        tk.Label(lf_row, text="Label this position:",
                 font=("Arial", 8), fg="#555").pack(side=tk.LEFT, padx=(2, 6))
        for status, text, bg in [
            ("green",  "● Memorized",  "#2a6e2a"),
            ("yellow", "● In prep",    "#b07a10"),
            ("red",    "● Off-book",   "#8b0000"),
            ("blue",   "● Alternative","#1a4a8a"),
        ]:
            lbl = tk.Label(lf_row, text=text, bg=bg, fg="white",
                           font=("Arial", 8, "bold"), padx=6, pady=3, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=2)
            lbl.bind("<Button-1>", lambda _, s=status: self._drill_set_prep(s))
        skip_lbl = tk.Label(lf_row, text="Skip →", bg="#dddddd", fg="#444",
                            font=("Arial", 8), padx=6, pady=3, cursor="hand2")
        skip_lbl.pack(side=tk.LEFT, padx=2)
        skip_lbl.bind("<Button-1>", lambda _: self._drill_skip_label())

        # Correction buttons — shown only after a mistake
        self._correction_frame = tk.Frame(dr)
        self._drill_book_lbl = tk.Label(
            self._correction_frame, text="Play book move",
            bg="#2a6e2a", fg="white", font=("Arial", 10, "bold"),
            padx=8, pady=5, cursor="hand2",
        )
        self._drill_book_lbl.pack(fill=tk.X, pady=2)
        self._drill_book_lbl.bind("<Button-1>", lambda _: self._drill_play_book_move())

        self._drill_update_lbl = tk.Label(
            self._correction_frame, text="Update book to my move",
            bg="#b07a10", fg="white", font=("Arial", 10, "bold"),
            padx=8, pady=5, cursor="hand2",
        )
        self._drill_update_lbl.pack(fill=tk.X, pady=2)
        self._drill_update_lbl.bind("<Button-1>", lambda _: self._drill_update_book())

        self._drill_alt_lbl = tk.Label(
            self._correction_frame, text="Mark my move as Alternative",
            bg="#1a4a8a", fg="white", font=("Arial", 10, "bold"),
            padx=8, pady=5, cursor="hand2",
        )
        self._drill_alt_lbl.pack(fill=tk.X, pady=2)
        self._drill_alt_lbl.bind("<Button-1>", lambda _: self._drill_update_alternative())

        ttk.Separator(dr).pack(fill="x", pady=4)

        # Line-management buttons
        line_mgmt = tk.Frame(dr)
        line_mgmt.pack(fill=tk.X, pady=(0, 2))

        self._stop_line_lbl = tk.Label(
            line_mgmt, text="End line here",
            bg="#8b0000", fg="white", font=("Arial", 9, "bold"),
            padx=6, pady=4, cursor="hand2",
        )
        self._stop_line_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self._stop_line_lbl.bind("<Button-1>", lambda _: self._drill_toggle_stop())

        self._extend_line_lbl = tk.Label(
            line_mgmt, text="Extend line",
            bg="#1a5276", fg="white", font=("Arial", 9, "bold"),
            padx=6, pady=4, cursor="hand2",
        )
        self._extend_line_lbl.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(2, 0))
        self._extend_line_lbl.bind("<Button-1>", lambda _: self._drill_extend_line())

        ttk.Separator(dr).pack(fill="x", pady=4)

        bottom = tk.Frame(dr)
        bottom.pack(fill=tk.X)
        tk.Button(bottom, text="New Game", command=self._drill_new_game,
                  font=("Arial", 9), width=10).pack(side=tk.LEFT, padx=2)
        tk.Button(bottom, text="End Drill", command=self._end_drill,
                  font=("Arial", 9), width=10).pack(side=tk.RIGHT, padx=2)

        self._drill_alternate_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            dr, text="Alternate colors each game",
            variable=self._drill_alternate_var,
            font=("Arial", 9),
        ).pack(anchor="w", padx=4, pady=(4, 0))

        self._drill_include_alt_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            dr, text="Include alternative moves",
            variable=self._drill_include_alt_var,
            font=("Arial", 9),
        ).pack(anchor="w", padx=4)

        filter_lbl = tk.Label(
            dr, text="Filter Openings",
            bg="#2a4a6e", fg="white", font=("Arial", 9, "bold"),
            padx=6, pady=4, cursor="hand2",
        )
        filter_lbl.pack(fill=tk.X, pady=(4, 0))
        filter_lbl.bind("<Button-1>", lambda _: self._drill_show_filter_dialog())

        self._drill_filter_var = tk.StringVar(value="No opening filter active")
        self._drill_filter_lbl = tk.Label(
            dr, textvariable=self._drill_filter_var,
            font=("Arial", 8), fg="#888888",
            wraplength=290, justify="left",
        )
        self._drill_filter_lbl.pack(anchor="w", padx=4, pady=(1, 0))

        review_here_lbl = tk.Label(
            dr, text="Review this position",
            bg="#555555", fg="white", font=("Arial", 9),
            padx=6, pady=4, cursor="hand2",
        )
        review_here_lbl.pack(fill=tk.X, pady=(4, 0))
        review_here_lbl.bind("<Button-1>", lambda _: self._drill_goto_review())

        # ── Moves notation + opening name (below buttons, above engine) ─────
        ttk.Separator(dr).pack(fill="x", pady=(6, 2))

        self._drill_opening_var = tk.StringVar(value="")
        self._drill_opening_lbl = tk.Label(
            dr, textvariable=self._drill_opening_var,
            font=("Arial", 9, "italic"), fg="#5a4a8a",
            wraplength=290, justify="left",
        )
        self._drill_opening_lbl.pack(anchor="w", padx=4)

        notation_frame = tk.Frame(dr)
        notation_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 0))
        notation_sb = tk.Scrollbar(notation_frame, orient=tk.VERTICAL)
        self._drill_notation_txt = tk.Text(
            notation_frame,
            height=10,
            font=("Courier", 12),
            wrap=tk.NONE,
            state=tk.DISABLED,
            fg="black",
            bg="#ffffff",
            relief=tk.FLAT,
            cursor="arrow",
            yscrollcommand=notation_sb.set,
        )
        self._drill_notation_txt.tag_configure("row_even", background="#ffffff", foreground="black")
        self._drill_notation_txt.tag_configure("row_odd",  background="#ebebeb", foreground="black")
        self._drill_notation_txt.tag_configure("prep_green",  foreground="#1a6e1a")
        self._drill_notation_txt.tag_configure("prep_yellow", foreground="#7a5000")
        self._drill_notation_txt.tag_configure("prep_red",    foreground="#8b0000")
        self._drill_notation_txt.tag_configure("prep_blue",   foreground="#1a4a8a")
        for _t in ("prep_green", "prep_yellow", "prep_red", "prep_blue"):
            self._drill_notation_txt.tag_raise(_t)
        notation_sb.config(command=self._drill_notation_txt.yview)
        self._drill_notation_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        notation_sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Board drawing ────────────────────────────────────────────────────────

    def _sq_coords(self, sq: chess.Square) -> tuple[int, int, int, int]:
        """Return (x1, y1, x2, y2) canvas coords for a square, flipped for black if needed."""
        file = chess.square_file(sq)
        rank = chess.square_rank(sq)
        if self.repertoire == "black":
            file = 7 - file
            rank = 7 - rank
        x1 = file * SQUARE_PX
        y1 = (7 - rank) * SQUARE_PX
        return x1, y1, x1 + SQUARE_PX, y1 + SQUARE_PX

    def _draw_piece(self, piece: chess.Piece, cx: int, cy: int) -> None:
        img = self._piece_images.get((piece.piece_type, piece.color))
        if img:
            self.canvas.create_image(cx, cy, image=img, anchor="center")

    def _draw_board(self, exclude_sq: chess.Square | None = None) -> None:
        self.canvas.delete("all")
        last_sqs = set(self._last_move_sqs) if self._last_move_sqs else set()

        for sq in chess.SQUARES:
            rank, file = divmod(sq, 8)
            x1, y1, x2, y2 = self._sq_coords(sq)

            if sq == self._selected_sq or sq == self._drag_from_sq:
                color = HIGHLIGHT_SQ
            elif sq in self._drill_hint_sqs:
                color = "#e0a000"
            elif sq in last_sqs:
                color = LAST_MOVE_SQ
            elif (rank + file) % 2 == 0:
                color = LIGHT_SQ
            else:
                color = DARK_SQ

            self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="")

            # Rank/file labels
            if (self.repertoire == "white" and file == 0) or (self.repertoire == "black" and file == 7):
                lbl_color = DARK_SQ if color == LIGHT_SQ else LIGHT_SQ
                self.canvas.create_text(x1 + 3, y1 + 3, text=str(rank + 1),
                                        anchor="nw", font=("Arial", 7), fill=lbl_color)
            bottom_rank = 0 if self.repertoire == "white" else 7
            if rank == bottom_rank:
                lbl_color = DARK_SQ if color == LIGHT_SQ else LIGHT_SQ
                self.canvas.create_text(x2 - 3, y2 - 3, text="abcdefgh"[file],
                                        anchor="se", font=("Arial", 7), fill=lbl_color)

            piece = self.board.piece_at(sq)
            if piece and sq != exclude_sq:
                self._draw_piece(piece, (x1 + x2) // 2, (y1 + y2) // 2)

    # ── State loading ────────────────────────────────────────────────────────

    def _sync_color_button(self) -> None:
        other = "Black" if self.repertoire == "white" else "White"
        self._color_lbl.config(text=f"Switch to {other}")

    def _toggle_repertoire_color(self) -> None:
        self.repertoire = "black" if self.repertoire == "white" else "white"
        self._original_repertoire = self.repertoire
        self.board = chess.Board()
        self.history.clear()
        self._forward_stack.clear()
        self._last_move_sqs = None
        self.root.title(f"Repertoire Review — {self.repertoire.capitalize()}")
        self._sync_color_button()
        self._load_position()

    def _load_position(self) -> None:
        conn = get_connection(self.db_path)
        try:
            self.current_node = get_node(conn, normalize_fen(self.board), self.repertoire)
        finally:
            conn.close()
        self._refresh()

    def _refresh(self) -> None:
        self._draw_board()
        self._populate_moves_list()
        self._review_update_notation()
        self._update_gap_warning()
        self.status_var.set(f"Ply {len(self.history)} | {self.repertoire.capitalize()} repertoire")

    def _is_my_turn(self) -> bool:
        return (self.board.turn == chess.WHITE) == (self.repertoire == "white")

    def _populate_moves_list(self) -> None:
        self.moves_lb.delete(0, tk.END)
        if not self.current_node:
            return
        if self._is_my_turn():
            self.moves_label.config(text="Your move:", fg="#2a6e2a")
            best = self.current_node.best_move()
            if best:
                self.moves_lb.insert(tk.END, best.summary())
            else:
                self.moves_lb.insert(tk.END, "(no move recorded)")
        else:
            self.moves_label.config(text="Opponent moves seen:", fg="#333333")
            for m in sorted(self.current_node.opponent_moves, key=lambda x: x.frequency, reverse=True):
                self.moves_lb.insert(tk.END, m.summary())

    def _review_update_notation(self) -> None:
        """Rebuild the review-mode move history notation widget with prep color coding."""
        move_info: dict[tuple[str, str], tuple[bool, str | None]] = {}
        if self.history:
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    """SELECT p.fen, m.uci, m.is_my_move, m.prep_status
                       FROM moves m
                       JOIN positions p ON p.id = m.from_position_id
                       WHERE m.repertoire = ?""",
                    (self.repertoire,),
                ).fetchall()
                move_info = {
                    (r["fen"], r["uci"]): (bool(r["is_my_move"]), r["prep_status"])
                    for r in rows
                }
            finally:
                conn.close()

        txt = self._review_notation_txt
        txt.config(state=tk.NORMAL)
        txt.delete("1.0", tk.END)

        board = chess.Board()
        i = 0
        move_num = 1
        while i < len(self.history):
            white_fen = normalize_fen(board)
            white_uci = self.history[i].uci()
            white_san = board.san(self.history[i])
            board.push(self.history[i])
            i += 1

            black_fen = black_uci = black_san = ""
            if i < len(self.history):
                black_fen = normalize_fen(board)
                black_uci = self.history[i].uci()
                black_san = board.san(self.history[i])
                board.push(self.history[i])
                i += 1

            row_tag = "row_odd" if move_num % 2 == 1 else "row_even"
            line = f"  {move_num:>2}.  {white_san:<8}  {black_san}\n"
            txt.insert(tk.END, line, row_tag)

            w_is_my, w_prep = move_info.get((white_fen, white_uci), (False, None))
            if w_is_my and w_prep:
                txt.tag_add(f"prep_{w_prep}", f"{move_num}.7", f"{move_num}.{7 + len(white_san)}")

            if black_san:
                b_is_my, b_prep = move_info.get((black_fen, black_uci), (False, None))
                if b_is_my and b_prep:
                    txt.tag_add(f"prep_{b_prep}", f"{move_num}.17", f"{move_num}.{17 + len(black_san)}")

            move_num += 1

        txt.config(state=tk.DISABLED)
        txt.see(tk.END)

    def _update_gap_warning(self) -> None:
        if self.current_node and self.current_node.has_coverage_gap():
            turn = "white" if self.board.turn == chess.WHITE else "black"
            if (turn == "white") == (self.repertoire == "white"):
                self.gap_var.set("⚠ Coverage gap — no clear preferred move here")
                return
        self.gap_var.set("")

    # ── Interaction ──────────────────────────────────────────────────────────

    def _sq_from_xy(self, x: int, y: int) -> chess.Square | None:
        file = x // SQUARE_PX
        rank = 7 - (y // SQUARE_PX)
        if self.repertoire == "black":
            file = 7 - file
            rank = 7 - rank
        if 0 <= file < 8 and 0 <= rank < 8:
            return chess.square(file, rank)
        return None

    def _on_drag_start(self, event: tk.Event) -> None:
        sq = self._sq_from_xy(event.x, event.y)
        if sq is None:
            return
        piece = self.board.piece_at(sq)
        if not piece or piece.color != self.board.turn:
            return
        self._drag_from_sq = sq
        # Redraw board without the lifted piece, then draw it floating at cursor
        self._draw_board(exclude_sq=sq)
        img = self._piece_images.get((piece.piece_type, piece.color))
        if img:
            self._drag_img_id = self.canvas.create_image(event.x, event.y, image=img, anchor="center")

    def _on_drag_motion(self, event: tk.Event) -> None:
        if self._drag_img_id is not None:
            self.canvas.coords(self._drag_img_id, event.x, event.y)

    def _on_drag_release(self, event: tk.Event) -> None:
        if self._drag_from_sq is None:
            return
        # Clean up floating image
        if self._drag_img_id is not None:
            self.canvas.delete(self._drag_img_id)
            self._drag_img_id = None
        from_sq = self._drag_from_sq
        self._drag_from_sq = None
        to_sq = self._sq_from_xy(event.x, event.y)
        if to_sq is not None and to_sq != from_sq:
            if self._drill_mode and not self._drill_awaiting_correction:
                self._drill_check_move(from_sq, to_sq)
            elif not self._drill_mode:
                self._try_move(from_sq, to_sq)
        else:
            self._draw_board()  # snap back

    def _try_move(self, from_sq: chess.Square, to_sq: chess.Square) -> None:
        move = chess.Move(from_sq, to_sq)
        piece = self.board.piece_at(from_sq)
        if piece and piece.piece_type == chess.PAWN:
            to_rank = chess.square_rank(to_sq)
            if to_rank in (0, 7):
                move = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)
        if move not in self.board.legal_moves:
            self._draw_board()
            return
        self._navigate_move(move, from_db=False)

    def _navigate_move(self, move: chess.Move, from_db: bool = True, _via_forward: bool = False) -> None:
        """Push a move and update state. from_db=True means it came from the moves list."""
        if not _via_forward:
            self._forward_stack.clear()

        edge: MoveEdge | None = None
        if self.current_node:
            for e in self.current_node.moves:
                if e.uci == move.uci():
                    edge = e
                    break

        self._last_move_sqs = (move.from_square, move.to_square)
        self.history.append(move)
        self.board.push(move)
        self.last_edge = edge
        self._load_position()

        if edge:
            self._show_edge_info(edge)
        if not self._drill_mode:
            self._review_load_prep_status()

    def _on_move_select(self, _event: tk.Event) -> None:
        edge = self._selected_edge()
        if edge:
            self._show_edge_info(edge)

    def _on_move_navigate(self, _event: tk.Event) -> None:
        edge = self._selected_edge()
        if edge:
            move = chess.Move.from_uci(edge.uci)
            if move in self.board.legal_moves:
                self._navigate_move(move)

    def _selected_edge(self) -> MoveEdge | None:
        sel = self.moves_lb.curselection()
        if not sel or not self.current_node:
            return None
        idx = sel[0]
        sorted_moves = sorted(self.current_node.moves, key=lambda x: x.frequency, reverse=True)
        return sorted_moves[idx] if idx < len(sorted_moves) else None

    def _show_edge_info(self, edge: MoveEdge) -> None:
        self.notes_txt.delete("1.0", tk.END)
        self.notes_txt.insert("1.0", edge.notes)
        self.tags_var.set(", ".join(edge.tags))
        eval_str = f"Engine: {edge.engine_eval:+.0f}cp" if edge.engine_eval is not None else ""
        if edge.flags:
            eval_str += f"  [{', '.join(edge.flags)}]"
        self.eval_var.set(eval_str)

    def _back(self) -> None:
        if self._drill_mode:
            self._drill_go_back()
        else:
            self._go_back()

    def _go_back(self) -> None:
        if self.history:
            self._forward_stack.append(self.history.pop())
            self.board.pop()
            self.last_edge = None
            self._last_move_sqs = None
            self._load_position()
            self._review_load_prep_status()

    def _drill_go_back(self) -> None:
        if not self.history:
            return
        # Cancel correction state and un-count the failed attempt
        if self._drill_awaiting_correction:
            self._drill_awaiting_correction = False
            self._correction_frame.pack_forget()
            if not self._drill_main_vs_alt:
                self._drill_total = max(0, self._drill_total - 1)
        self._drill_main_vs_alt = False
        self._drill_book_lbl.config(text="Play book move", bg="#2a6e2a")
        self._drill_update_lbl.pack(fill=tk.X, pady=2)
        self._drill_alt_lbl.pack(fill=tk.X, pady=2)
        self._drill_label_frame.pack_forget()

        # Pop until it's the player's turn again (or history is empty).
        # In the white repertoire white moves at even plies (0, 2, …);
        # in the black repertoire black moves at odd plies (1, 3, …).
        my_parity = 0 if self.repertoire == "white" else 1
        while self.history:
            self.history.pop()
            self.board.pop()
            if len(self.history) % 2 == my_parity:
                break

        self._last_move_sqs = None
        self._drill_hint_sqs = set()
        self._draw_board()
        self._drill_update_notation()
        self._drill_load_node()

    def _go_forward(self) -> None:
        if self._forward_stack:
            move = self._forward_stack.pop()
            if move in self.board.legal_moves:
                self._navigate_move(move, _via_forward=True)

    def _go_start(self) -> None:
        while self.history:
            self.history.pop()
            self.board.pop()
        self.last_edge = None
        self._last_move_sqs = None
        self._load_position()

    def _save_annotation(self) -> None:
        edge = self._selected_edge() or self.last_edge
        if edge is None:
            messagebox.showinfo("No move selected", "Navigate to or select a move first.")
            return

        notes = self.notes_txt.get("1.0", tk.END).strip()
        tags_raw = self.tags_var.get().strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        # Reconstruct the move sequence to reach the current edge
        moves_to_edge = [m.uci() for m in self.history]
        if edge is self.last_edge:
            # Already navigated; history ends with the move to annotate
            pass
        else:
            moves_to_edge = moves_to_edge + [edge.uci]

        if not moves_to_edge:
            messagebox.showinfo("Nothing to annotate", "No move sequence available.")
            return

        ok = update_annotation(moves_to_edge, self.repertoire, notes=notes, tags=tags or None,
                               db_path=self.db_path)
        if ok:
            edge.notes = notes
            edge.tags = tags
            self.status_var.set("Annotation saved.")
        else:
            messagebox.showerror("Not found", "Move not found in DB — annotate imported moves only.")

    def _show_gaps(self) -> None:
        gaps = find_coverage_gaps(self.repertoire, self.db_path)
        if not gaps:
            messagebox.showinfo("No gaps", "No coverage gaps found. Repertoire looks solid!")
            return

        win = tk.Toplevel(self.root)
        win.title(f"Coverage Gaps — {self.repertoire.capitalize()}")
        win.geometry("600x400")

        tk.Label(win, text=f"{len(gaps)} positions with no clear preferred move:",
                 font=("Arial", 10, "bold")).pack(anchor="w", padx=8, pady=4)

        frame = tk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=8)
        sb = tk.Scrollbar(frame)
        lb = tk.Listbox(frame, font=("Courier", 9), yscrollcommand=sb.set)
        sb.config(command=lb.yview)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        for fen, node in gaps:
            best = node.best_move()
            best_str = f"best={best.san}" if best else "no moves"
            lb.insert(tk.END, f"{fen}  ({best_str}, {len(node.my_moves)} moves)")

        tk.Label(win, text="Copy a FEN to analyse it in your chess GUI.",
                 font=("Arial", 8), fg="#888").pack(pady=4)


    # ── Drill ────────────────────────────────────────────────────────────────

    def _start_drill(self) -> None:
        init_drill_db(self.db_path)
        self._original_repertoire = self.repertoire
        self._drill_game_number = 0
        self._drill_mode = True
        self._drill_correct = 0
        self._drill_total = 0
        self._drill_awaiting_correction = False
        self._drill_book_move = None
        self._drill_hint_sqs = set()
        self._color_lbl.pack_forget()
        self._review_frame.pack_forget()
        self._drill_frame.pack(fill=tk.BOTH, expand=True)
        self._drill_new_game()

    def _end_drill(self) -> None:
        self._drill_mode = False
        self.repertoire = self._original_repertoire
        self._correction_frame.pack_forget()
        self._drill_frame.pack_forget()
        self._review_frame.pack(fill=tk.BOTH, expand=True)
        self.board = chess.Board()
        self.history.clear()
        self._forward_stack.clear()
        self._last_move_sqs = None
        self.root.title(f"Repertoire Review — {self.repertoire.capitalize()}")
        self._sync_color_button()
        self._color_lbl.pack(side=tk.RIGHT, padx=2)
        self._load_position()

    def _drill_goto_review(self) -> None:
        """Switch to review mode at the current drill position."""
        self._drill_mode = False
        self._drill_awaiting_correction = False
        self.repertoire = self._original_repertoire
        self._correction_frame.pack_forget()
        self._drill_frame.pack_forget()
        self._review_frame.pack(fill=tk.BOTH, expand=True)
        self._forward_stack.clear()
        self.root.title(f"Repertoire Review — {self.repertoire.capitalize()}")
        self._sync_color_button()
        self._color_lbl.pack(side=tk.RIGHT, padx=2)
        self._load_position()

    def _drill_new_game(self) -> None:
        # Alternate colors only when the checkbox is checked
        self._drill_game_number += 1
        if self._drill_alternate_var.get():
            self.repertoire = "white" if self._drill_game_number % 2 == 1 else "black"
        color_label = "White" if self.repertoire == "white" else "Black"
        self._drill_header_var.set(f"Drill Mode — Playing {color_label}")
        self.root.title(f"Repertoire Drill — {color_label}")

        self.board = chess.Board()
        self.history.clear()
        self._forward_stack.clear()
        self._last_move_sqs = None
        self._drill_awaiting_correction = False
        self._drill_book_move = None
        self._drill_prep_status = None
        self._drill_main_vs_alt = False
        self._drill_hint_sqs = set()
        self._drill_book_lbl.config(text="Play book move", bg="#2a6e2a")
        self._drill_update_lbl.pack(fill=tk.X, pady=2)
        self._drill_alt_lbl.pack(fill=tk.X, pady=2)
        self._correction_frame.pack_forget()
        self._drill_label_frame.pack_forget()
        self._drill_feedback_var.set("Game started.")
        self._drill_feedback_lbl.config(fg="#333333")
        self._drill_progress_var.set(
            f"{self._drill_correct} correct  •  {self._drill_total} attempted"
        )
        self._draw_board()
        self._drill_update_notation()
        self._drill_load_node()

    def _drill_load_node(self) -> None:
        self._drill_book_move = None
        self._drill_book_move_id = None
        self._drill_book_move_ef = 2.5
        self._drill_book_move_interval = 1
        today = date.today().isoformat()
        cur_fen = normalize_fen(self.board)
        conn = get_connection(self.db_path)
        try:
            self.current_node = get_node(conn, cur_fen, self.repertoire)
            is_stop = conn.execute(
                "SELECT 1 FROM drill_stops WHERE fen = ? AND repertoire = ?",
                (cur_fen, self.repertoire),
            ).fetchone() is not None
            if self._is_my_turn() and self.current_node:
                my_moves = self.current_node.my_moves
                valid_filter = self._drill_filter_fens.get(self.repertoire)
                if valid_filter:
                    filtered_my = [m for m in my_moves if m.to_fen in valid_filter]
                    if filtered_my:
                        my_moves = filtered_my
                # Always exclude red-labeled off-book moves — never a drill target
                red_ucis = set(
                    r["uci"] for r in conn.execute(
                        """SELECT uci FROM moves
                           WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                             AND repertoire = ? AND is_my_move = 1
                             AND prep_status = 'red'""",
                        (cur_fen, self.repertoire),
                    ).fetchall()
                )
                my_moves = [m for m in my_moves if m.uci not in red_ucis]
                if not self._drill_include_alt_var.get():
                    # Exclude blue-labeled alternative moves unless the setting is on
                    blue_ucis = set(
                        r["uci"] for r in conn.execute(
                            """SELECT uci FROM moves
                               WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                                 AND repertoire = ? AND is_my_move = 1
                                 AND prep_status = 'blue'""",
                            (cur_fen, self.repertoire),
                        ).fetchall()
                    )
                    my_moves = [m for m in my_moves if m.uci not in blue_ucis]
                best = max(my_moves, key=lambda m: m.frequency) if my_moves else None
                if best:
                    row = conn.execute(
                        """SELECT m.id, m.prep_status,
                                  COALESCE(ds.ease_factor, 2.5)    AS ef,
                                  COALESCE(ds.interval_days, 1)    AS iv
                           FROM moves m
                           LEFT JOIN drill_stats ds ON ds.move_id = m.id
                           WHERE m.from_position_id = (SELECT id FROM positions WHERE fen = ?)
                             AND m.uci = ? AND m.repertoire = ?""",
                        (cur_fen, best.uci, self.repertoire),
                    ).fetchone()
                    if row:
                        self._drill_book_move = chess.Move.from_uci(best.uci)
                        self._drill_book_move_id = row["id"]
                        self._drill_book_move_ef = row["ef"]
                        self._drill_book_move_interval = row["iv"]
                        self._drill_prep_status = row["prep_status"]
                    else:
                        self._drill_book_move = chess.Move.from_uci(best.uci)
                        self._drill_book_move_id = None
                        self._drill_book_move_ef = 2.5
                        self._drill_book_move_interval = 1
                        self._drill_prep_status = None
        finally:
            conn.close()

        self._drill_label_frame.pack_forget()

        # Update "End line here" button to reflect current stop status
        stop_label = "★ Stop set — click to remove" if is_stop else "End line here"
        stop_bg = "#555555" if is_stop else "#8b0000"
        self._stop_line_lbl.config(text=stop_label, bg=stop_bg)

        if not self._is_my_turn():
            self.root.after(600, self._drill_play_computer)
            return

        if is_stop:
            self._drill_line_complete(reason="stop")
            return
        if self.current_node is None or self._drill_book_move is None:
            self._drill_line_complete(reason="end")
            return

        self._drill_feedback_var.set("Your turn — drag a piece to move.")
        self._drill_feedback_lbl.config(fg="#333333")

    def _drill_play_computer(self) -> None:
        """Pick the opponent move that steers toward the most overdue player position."""
        if self.current_node is None:
            self._drill_line_complete()
            return
        opp_moves = self.current_node.opponent_moves
        # Restrict to filtered openings when active
        valid_filter = self._drill_filter_fens.get(self.repertoire)
        if valid_filter:
            filtered = [e for e in opp_moves if e.to_fen in valid_filter]
            if filtered:
                opp_moves = filtered
        if not opp_moves:
            self._drill_line_complete()
            return

        today = date.today().isoformat()
        best_key: tuple | None = None
        best_edges: list = []

        conn = get_connection(self.db_path)
        try:
            for edge in opp_moves:
                # Drill stops are never reviewed so their due_date stays NULL —
                # that scores as maximally urgent and causes the same stopped line
                # to be chosen every game.  Deprioritise them.
                is_stop = conn.execute(
                    "SELECT 1 FROM drill_stops WHERE fen = ? AND repertoire = ?",
                    (edge.to_fen, self.repertoire),
                ).fetchone() is not None
                if is_stop:
                    due_count = 0
                    min_due = "9999-99-99"
                else:
                    row = conn.execute(
                        """SELECT
                               SUM(CASE WHEN ds.due_date IS NULL OR ds.due_date <= ? THEN 1 ELSE 0 END) AS due_count,
                               MIN(COALESCE(ds.due_date, '0000-00-00')) AS min_due
                           FROM moves m
                           LEFT JOIN drill_stats ds ON ds.move_id = m.id
                           WHERE m.from_position_id = (SELECT id FROM positions WHERE fen = ?)
                             AND m.repertoire = ? AND m.is_my_move = 1""",
                        (today, edge.to_fen, self.repertoire),
                    ).fetchone()
                    due_count = row["due_count"] or 0
                    min_due = row["min_due"] or "9999-99-99"

                # key: (has_due_move, min_due_date) — higher is more urgent
                # has_due=True beats False; for equal booleans, earlier date wins
                key = (due_count > 0, min_due)
                if best_key is None:
                    best_key = key
                    best_edges = [edge]
                elif key[0] and not best_key[0]:
                    best_key = key
                    best_edges = [edge]
                elif key[0] == best_key[0] and key[1] < best_key[1]:
                    best_key = key
                    best_edges = [edge]
                elif key == best_key:
                    best_edges.append(edge)
        finally:
            conn.close()

        # Among equally-urgent edges, weight by frequency so common lines
        # are drilled more often than obscure ones.
        def _freq_weights(edges: list) -> list[int]:
            return [max(1, e.frequency) for e in edges]

        chosen = random.choices(best_edges, weights=_freq_weights(best_edges))[0] if best_edges else None
        if chosen is None:
            chosen = random.choices(opp_moves, weights=_freq_weights(opp_moves))[0]

        move = chess.Move.from_uci(chosen.uci)
        if move not in self.board.legal_moves:
            self._drill_line_complete()
            return
        self._last_move_sqs = (move.from_square, move.to_square)
        self.history.append(move)
        self.board.push(move)
        self._draw_board()
        self._drill_update_notation()
        self._drill_load_node()

    def _drill_check_move(self, from_sq: chess.Square, to_sq: chess.Square) -> None:
        move = chess.Move(from_sq, to_sq)
        piece = self.board.piece_at(from_sq)
        if piece and piece.piece_type == chess.PAWN and chess.square_rank(to_sq) in (0, 7):
            move = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)
        if move not in self.board.legal_moves:
            self._draw_board()
            return

        book_move = self._drill_book_move
        book_san = ""
        if book_move:
            try:
                book_san = self.board.san(book_move)
            except Exception:
                book_san = book_move.uci()

        self._drill_total += 1
        pre_move_board = self.board.copy()  # capture before push for post-mistake analysis
        self._last_move_sqs = (from_sq, to_sq)
        self.history.append(move)
        self.board.push(move)
        self._draw_board()
        self._drill_update_notation()

        correct = book_move is not None and move.uci() == book_move.uci()
        # Red (off-book) moves are never correct, regardless of frequency
        if correct and self._drill_prep_status == "red":
            correct = False

        # Check if a non-book move is labeled as a blue alternative
        is_alternative = False
        alt_move_id: int | None = None
        alt_ef, alt_iv = 2.5, 1
        # Check what the played move is labeled, and whether book move is blue
        is_main_vs_alt = False  # player played main-line when book move is an alternative
        if not correct:
            prev_board = chess.Board()
            for m in self.history[:-1]:
                prev_board.push(m)
            from_fen = normalize_fen(prev_board)
            conn = get_connection(self.db_path)
            try:
                alt_row = conn.execute(
                    """SELECT m.id, m.prep_status,
                              COALESCE(ds.ease_factor, 2.5)  AS ef,
                              COALESCE(ds.interval_days, 1)  AS iv
                       FROM moves m
                       LEFT JOIN drill_stats ds ON ds.move_id = m.id
                       WHERE m.from_position_id = (SELECT id FROM positions WHERE fen = ?)
                         AND m.uci = ? AND m.repertoire = ? AND m.is_my_move = 1""",
                    (from_fen, move.uci(), self.repertoire),
                ).fetchone()
            finally:
                conn.close()
            if alt_row and alt_row["prep_status"] == "red":
                pass  # Red move played — always incorrect; no alternative credit
            elif alt_row and alt_row["prep_status"] == "blue":
                is_alternative = True
                alt_move_id = alt_row["id"]
                alt_ef, alt_iv = alt_row["ef"], alt_row["iv"]
            elif (self._drill_prep_status == "blue"
                  and self._drill_include_alt_var.get()
                  and alt_row and alt_row["prep_status"] in ("green", "yellow")):
                # Book move is the alternative; player chose the main line instead
                is_main_vs_alt = True
                alt_move_id = alt_row["id"]
                alt_ef, alt_iv = alt_row["ef"], alt_row["iv"]

        promoted = False
        if is_alternative and alt_move_id is not None:
            record_result(alt_move_id, True, alt_ef, alt_iv, self.db_path)
        elif is_main_vs_alt and alt_move_id is not None:
            record_result(alt_move_id, True, alt_ef, alt_iv, self.db_path)
        elif self._drill_book_move_id is not None:
            promoted = record_result(
                self._drill_book_move_id, correct,
                self._drill_book_move_ef, self._drill_book_move_interval,
                self.db_path,
            )

        if correct or is_alternative or is_main_vs_alt:
            self._drill_correct += 1
            if promoted:
                self._drill_prep_status = "green"
                self._drill_feedback_var.set("✓  New move learned!")
                self._drill_feedback_lbl.config(fg="#1a6e1a")
                self._drill_update_notation()
            elif is_alternative or self._drill_prep_status == "blue":
                self._drill_feedback_var.set("✓  Alternative!")
                self._drill_feedback_lbl.config(fg="#2a6e2a")
            elif is_main_vs_alt:
                self._drill_feedback_var.set("✓  Correct!")
                self._drill_feedback_lbl.config(fg="#1a4a8a")  # blue font
            else:
                self._drill_feedback_var.set("✓  Correct!")
                self._drill_feedback_lbl.config(fg="#2a6e2a")
            self._drill_progress_var.set(
                f"{self._drill_correct} correct  •  {self._drill_total} attempted"
            )
            if is_main_vs_alt:
                # Offer the player a chance to also see the alternative line
                self._drill_main_vs_alt = True
                self._drill_awaiting_correction = True
                self._drill_book_lbl.config(text="Play alternative move", bg="#1a4a8a")
                self._drill_update_lbl.pack_forget()
                self._drill_alt_lbl.pack_forget()
                self._correction_frame.pack(fill=tk.X, pady=4)
            else:
                # Alternative moves are already labeled blue — skip the label prompt
                effective_id = self._drill_book_move_id if not is_alternative else None
                if effective_id is not None and self._drill_prep_status is None:
                    self._drill_label_frame.pack(fill=tk.X, pady=2)
                else:
                    self._drill_load_node()
        else:
            self._drill_awaiting_correction = True
            msg = f"✗  Book move was: {book_san}" if book_san else "✗  Not in book."
            self._drill_feedback_var.set(msg)
            self._drill_feedback_lbl.config(fg="#c0392b")
            self._drill_progress_var.set(
                f"{self._drill_correct} correct  •  {self._drill_total} attempted"
            )
            self._correction_frame.pack(fill=tk.X, pady=4)
            self._analyse_position(pre_move_board)

    def _drill_play_book_move(self) -> None:
        if not self._drill_awaiting_correction:
            return
        book_move = self._drill_book_move
        # Capture before _drill_load_node resets these to None
        saved_move_id = self._drill_book_move_id
        saved_prep_status = self._drill_prep_status
        self.board.pop()
        self.history.pop()
        if book_move and book_move in self.board.legal_moves:
            self._last_move_sqs = (book_move.from_square, book_move.to_square)
            self.history.append(book_move)
            self.board.push(book_move)
        self._drill_awaiting_correction = False
        self._drill_book_move = None
        self._drill_main_vs_alt = False
        self._drill_book_lbl.config(text="Play book move", bg="#2a6e2a")
        self._drill_update_lbl.pack(fill=tk.X, pady=2)
        self._drill_alt_lbl.pack(fill=tk.X, pady=2)
        self._correction_frame.pack_forget()
        self._draw_board()
        self._drill_update_notation()
        if saved_move_id is not None and saved_prep_status is None:
            # Restore the ID so _drill_set_prep targets the correct move.
            # Do NOT call _drill_load_node yet — that would schedule the computer
            # move immediately. Label/skip buttons will call it instead.
            self._drill_book_move_id = saved_move_id
            self._drill_label_frame.pack(fill=tk.X, pady=2)
        else:
            self._drill_load_node()

    def _drill_update_book(self) -> None:
        if not self._drill_awaiting_correction:
            return

        uci_moves = [m.uci() for m in self.history]
        cur_fen = normalize_fen(self.board)

        try:
            set_as_book_move(uci_moves, self.repertoire, self.db_path)
            auto_label_alternatives(self.db_path)
        except Exception as exc:
            self._drill_feedback_var.set(f"Update failed: {exc}")
            self._drill_feedback_lbl.config(fg="#c0392b")
            return

        self._drill_awaiting_correction = False
        self._drill_book_move = None
        self._correction_frame.pack_forget()
        self._drill_feedback_var.set("Book updated — scanning game history…")
        self._drill_feedback_lbl.config(fg="#b07a10")

        # Look up the move we just promoted to book move so we can offer labeling
        if self.history:
            from_board = chess.Board()
            for m in self.history[:-1]:
                from_board.push(m)
            from_fen = normalize_fen(from_board)
            uci = self.history[-1].uci()
            conn = get_connection(self.db_path)
            try:
                row = conn.execute(
                    """SELECT id, prep_status FROM moves
                       WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                         AND uci = ? AND repertoire = ?""",
                    (from_fen, uci, self.repertoire),
                ).fetchone()
            finally:
                conn.close()
            if row:
                self._drill_book_move_id = row["id"]
                self._drill_prep_status = row["prep_status"]

        # Build continuations in background so the UI stays responsive
        def _run() -> None:
            from updater import build_continuations_from_games
            n = build_continuations_from_games(
                cur_fen, uci_moves, self.repertoire, db_path=self.db_path
            )
            self.root.after(0, lambda: self._on_continuations_built(n))

        threading.Thread(target=_run, daemon=True).start()

    def _drill_update_alternative(self) -> None:
        if not self._drill_awaiting_correction:
            return

        uci_moves = [m.uci() for m in self.history]
        cur_fen = normalize_fen(self.board)

        try:
            add_manual_line(uci_moves, self.repertoire, db_path=self.db_path)
        except Exception as exc:
            self._drill_feedback_var.set(f"Update failed: {exc}")
            self._drill_feedback_lbl.config(fg="#c0392b")
            return

        # Label the player's move as blue (alternative)
        if self.history:
            from_board = chess.Board()
            for m in self.history[:-1]:
                from_board.push(m)
            from_fen = normalize_fen(from_board)
            uci = self.history[-1].uci()
            with transaction(self.db_path) as conn:
                conn.execute(
                    """UPDATE moves SET prep_status = 'blue'
                       WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                         AND uci = ? AND repertoire = ?""",
                    (from_fen, uci, self.repertoire),
                )
            conn = get_connection(self.db_path)
            try:
                row = conn.execute(
                    """SELECT id FROM moves
                       WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                         AND uci = ? AND repertoire = ?""",
                    (from_fen, uci, self.repertoire),
                ).fetchone()
            finally:
                conn.close()
            if row:
                self._drill_book_move_id = row["id"]
                self._drill_prep_status = "blue"

        self._drill_awaiting_correction = False
        self._drill_book_move = None
        self._correction_frame.pack_forget()
        self._drill_update_notation()
        self._drill_feedback_var.set("Alternative added — scanning game history…")
        self._drill_feedback_lbl.config(fg="#1a4a8a")
        self._refresh_openings_split_cache()

        def _run() -> None:
            from updater import build_continuations_from_games
            n = build_continuations_from_games(
                cur_fen, uci_moves, self.repertoire, db_path=self.db_path
            )
            self.root.after(0, lambda: self._on_continuations_built(n))

        threading.Thread(target=_run, daemon=True).start()

    def _on_continuations_built(self, n_games: int) -> None:
        if n_games > 0:
            self._drill_feedback_var.set(
                f"Book updated — imported continuations from {n_games} game(s). Continuing…"
            )
        else:
            self._drill_feedback_var.set(
                "Book updated — no prior games from this position. "
                "Use 'Extend line' to add continuations manually."
            )
        self._drill_feedback_lbl.config(fg="#b07a10")
        self._refresh_openings_split_cache()
        # If the promoted move has no label yet, show the prompt;
        # the label/skip buttons will call _drill_load_node to continue.
        if self._drill_book_move_id is not None and self._drill_prep_status is None:
            self._drill_label_frame.pack(fill=tk.X, pady=2)
        else:
            self.root.after(2500, self._drill_load_node)

    def _drill_line_complete(self, reason: str = "end") -> None:
        total = self._drill_total
        correct = self._drill_correct
        pct = f"{correct / total:.0%}" if total else "—"
        if reason == "stop":
            msg = f"Line stop reached.\n{correct} / {total} correct  ({pct})"
        else:
            msg = f"End of line.\n{correct} / {total} correct  ({pct})"
        self._drill_feedback_var.set(msg)
        self._drill_feedback_lbl.config(fg="#3a7abf")
        self._drill_progress_var.set(f"{correct} correct  •  {total} attempted")

    def _drill_toggle_stop(self) -> None:
        cur_fen = normalize_fen(self.board)
        now_stopped = toggle_drill_stop(cur_fen, self.repertoire, self.db_path)
        if now_stopped:
            self._stop_line_lbl.config(text="★ Stop set — click to remove", bg="#555555")
            self._drill_feedback_var.set("Stop set — drill will end here in future games.")
            self._drill_feedback_lbl.config(fg="#888")
            self._drill_line_complete(reason="stop")
        else:
            self._stop_line_lbl.config(text="End line here", bg="#8b0000")
            self._drill_feedback_var.set("Stop removed — this position will be drilled again.")
            self._drill_feedback_lbl.config(fg="#2a6e2a")

    def _drill_extend_line(self) -> None:
        moves_str = simpledialog.askstring(
            "Extend line",
            "Enter moves from current position in SAN (e.g. Nf3 d5 c4):",
            parent=self.root,
        )
        if not moves_str:
            return

        test_board = self.board.copy()
        uci_ext: list[str] = []
        for token in moves_str.strip().split():
            try:
                move = test_board.parse_san(token)
            except ValueError:
                try:
                    move = test_board.parse_uci(token)
                except ValueError:
                    self._drill_feedback_var.set(f"Invalid move: {token}")
                    self._drill_feedback_lbl.config(fg="#c0392b")
                    return
            uci_ext.append(move.uci())
            test_board.push(move)

        full_moves = [m.uci() for m in self.history] + uci_ext
        try:
            n = add_manual_line(full_moves, self.repertoire, db_path=self.db_path)
            self._drill_feedback_var.set(f"Added {n} move(s). Continuing…")
            self._drill_feedback_lbl.config(fg="#2a6e2a")
            self._drill_load_node()
        except Exception as exc:
            self._drill_feedback_var.set(f"Error: {exc}")
            self._drill_feedback_lbl.config(fg="#c0392b")


    def _add_line_to_book(self) -> None:
        if not self.history:
            self.status_var.set("Nothing to add — navigate some moves first.")
            self._flash_label(self._add_line_lbl, error=True)
            return
        try:
            n = add_manual_line(
                [m.uci() for m in self.history], self.repertoire, db_path=self.db_path
            )
            auto_label_alternatives(self.db_path)
            self._refresh_openings_split_cache()
            self.status_var.set(f"Added {n} move(s) to book.")
            self._flash_label(self._add_line_lbl, error=False)
            self._load_position()
        except Exception as exc:
            self.status_var.set(f"Error: {exc}")
            self._flash_label(self._add_line_lbl, error=True)

    def _last_my_move_index(self) -> int:
        """Return index of the last 'my move' in self.history, or -1 if none.
        White repertoire: my moves are at even indices (0, 2, 4…).
        Black repertoire: my moves are at odd indices (1, 3, 5…).
        """
        my_parity = 0 if self.repertoire == "white" else 1
        for i in range(len(self.history) - 1, -1, -1):
            if i % 2 == my_parity:
                return i
        return -1

    def _review_load_prep_status(self) -> None:
        idx = self._last_my_move_index()
        if idx < 0:
            self._review_prep_status = None
            self._review_refresh_prep_buttons()
            return
        prev_board = chess.Board()
        for m in self.history[:idx]:
            prev_board.push(m)
        from_fen = normalize_fen(prev_board)
        uci = self.history[idx].uci()
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                """SELECT prep_status FROM moves
                   WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                     AND uci = ? AND repertoire = ?""",
                (from_fen, uci, self.repertoire),
            ).fetchone()
        finally:
            conn.close()
        self._review_prep_status = row["prep_status"] if row else None
        self._review_refresh_prep_buttons()

    def _review_set_prep(self, status: str) -> None:
        idx = self._last_my_move_index()
        if idx < 0:
            self.status_var.set("No 'my move' in history yet.")
            return
        prev_board = chess.Board()
        for m in self.history[:idx]:
            prev_board.push(m)
        from_fen = normalize_fen(prev_board)
        san_board = chess.Board()
        for m in self.history[:idx]:
            san_board.push(m)
        uci = self.history[idx].uci()
        san = san_board.san(self.history[idx])
        prev_status = self._review_prep_status
        with transaction(self.db_path) as conn:
            updated = conn.execute(
                """UPDATE moves SET prep_status = ?
                   WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                     AND uci = ? AND repertoire = ?""",
                (status, from_fen, uci, self.repertoire),
            ).rowcount

        if updated == 0:
            # Move not in tree — ask before adding
            self._prompt_add_line_for_label(
                status, san, from_fen, uci, list(self.history[:idx + 1])
            )
            return

        self._apply_review_label(status, san, from_fen, uci, prev_status)

    def _prompt_add_line_for_label(
        self,
        status: str,
        san: str,
        from_fen: str,
        uci: str,
        history_to_move: list,
    ) -> None:
        labels = {"green": "Memorized", "yellow": "In prep", "red": "Off-book", "blue": "Alternative"}
        label_str = labels[status]

        win = tk.Toplevel(self.root)
        win.title("Move Not in Tree")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()

        tk.Label(
            win,
            text=f"'{san}' is not in the repertoire tree",
            font=("Arial", 11, "bold"),
        ).pack(padx=16, pady=(14, 4))
        tk.Label(
            win,
            text=(
                f"Would you like to add this line to the tree and label '{san}' "
                f"as {label_str.lower()}?"
            ),
            font=("Arial", 10),
            wraplength=380,
            justify="left",
        ).pack(padx=16, pady=(0, 10))

        def _add_and_label() -> None:
            add_manual_line(
                [m.uci() for m in history_to_move], self.repertoire, db_path=self.db_path
            )
            with transaction(self.db_path) as conn:
                conn.execute(
                    """UPDATE moves SET prep_status = ?
                       WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                         AND uci = ? AND repertoire = ?""",
                    (status, from_fen, uci, self.repertoire),
                )
            win.destroy()
            self._apply_review_label(status, san, from_fen, uci)

        def _cancel() -> None:
            win.destroy()

        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 14))
        for text, cmd, bg in [
            (f"Add to tree and label as {label_str}", _add_and_label, "#2a4a6e"),
            ("Keep out of tree",                       _cancel,        "#888888"),
        ]:
            lbl = tk.Label(btn_frame, text=text, bg=bg, fg="white",
                           font=("Arial", 9, "bold"), padx=10, pady=6, cursor="hand2")
            lbl.pack(fill=tk.X, pady=2)
            lbl.bind("<Button-1>", lambda _, c=cmd: c())

    def _apply_review_label(
        self, status: str, san: str, from_fen: str, uci: str, prev_status: str | None = None
    ) -> None:
        """Commit the label change to the UI and run follow-up checks."""
        self._review_prep_status = status
        self._review_refresh_prep_buttons()
        self._review_update_notation()
        labels = {"green": "Memorized", "yellow": "In prep", "red": "Off-book", "blue": "Alternative"}
        self.status_var.set(f"Labeled {san} as {labels[status]}.")
        if status in ("green", "yellow", "red"):
            conn = get_connection(self.db_path)
            try:
                id_row = conn.execute(
                    """SELECT id FROM moves
                       WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                         AND uci = ? AND repertoire = ?""",
                    (from_fen, uci, self.repertoire),
                ).fetchone()
            finally:
                conn.close()
            if id_row:
                if status == "red":
                    self._check_and_prompt_new_book_move(from_fen, id_row["id"], prev_status)
                else:
                    self._check_and_show_label_conflict(
                        id_row["id"], from_fen, san, status
                    )

    def _review_refresh_prep_buttons(self) -> None:
        for s, btn in self._review_prep_btns.items():
            if s == self._review_prep_status:
                btn.config(bg=btn._active_bg, fg="white")  # type: ignore[attr-defined]
            else:
                btn.config(bg="#cccccc", fg="#444444")

    def _flash_label(self, lbl: tk.Label, error: bool = False) -> None:
        orig_bg = lbl.cget("bg")
        orig_text = lbl.cget("text")
        flash_bg = "#c0392b" if error else "#1e8449"
        flash_text = "✗  Error" if error else "✓  Added!"
        lbl.config(bg=flash_bg, text=flash_text)
        lbl.after(1200, lambda: lbl.config(bg=orig_bg, text=orig_text))

    def _update_filter_label(self) -> None:
        all_names: list[str] = []
        for names in self._drill_filter.values():
            all_names.extend(sorted(names))
        if all_names:
            preview = ", ".join(all_names[:3])
            if len(all_names) > 3:
                preview += f" +{len(all_names) - 3} more"
            self._drill_filter_var.set(f"Filter: {preview}")
            self._drill_filter_lbl.config(fg="#b07a10")
        else:
            self._drill_filter_var.set("No opening filter active")
            self._drill_filter_lbl.config(fg="#888888")

    def _refresh_openings_split_cache(self) -> None:
        """Recompute the openings split in a background thread and store in cache."""
        import threading
        db_path = self.db_path

        def _run() -> None:
            result = {
                color: get_openings_split(color, db_path)
                for color in ("white", "black")
            }
            self.root.after(0, lambda: setattr(self, "_openings_split_cache", result))

        threading.Thread(target=_run, daemon=True).start()

    def _drill_show_filter_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Opening Filter")
        win.geometry("440x520")
        win.resizable(False, True)

        tk.Label(win, text="Restrict drill to selected openings:",
                 font=("Arial", 10, "bold")).pack(padx=10, pady=(10, 0), anchor="w")
        tk.Label(win, text="Leave all unchecked to drill all openings.",
                 font=("Arial", 8), fg="#888").pack(padx=10, anchor="w")

        notebook = ttk.Notebook(win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        check_vars: dict[tuple[str, str], tk.BooleanVar] = {}
        alt_keys: set[tuple[str, str]] = set()  # (color, name) pairs from alt tabs
        # Map tab index → its canvas so the win-level scroll handler knows which to scroll
        tab_canvases: dict[int, tk.Canvas] = {}

        # Use cached split if available, otherwise fetch synchronously (first open)
        split: dict[str, tuple[list, list]] = self._openings_split_cache or {
            color: get_openings_split(color, self.db_path)
            for color in ("white", "black")
        }

        tab_specs = [
            ("White",      "white", False),
            ("White Alt.", "white", True),
            ("Black",      "black", False),
            ("Black Alt.", "black", True),
        ]

        for tab_idx, (tab_label, color, is_alt) in enumerate(tab_specs):
            outer = tk.Frame(notebook)
            notebook.add(outer, text=tab_label)

            canvas = tk.Canvas(outer, highlightthickness=0)
            sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            inner = tk.Frame(canvas)
            tab_canvases[tab_idx] = canvas

            canvas.configure(yscrollcommand=sb.set)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_configure(event, c=canvas):
                c.configure(scrollregion=c.bbox("all"))
            inner.bind("<Configure>", _on_inner_configure)

            def _on_canvas_configure(event, c=canvas, w=win_id):
                c.itemconfig(w, width=event.width)
            canvas.bind("<Configure>", _on_canvas_configure)

            openings = split[color][1 if is_alt else 0]
            current = self._drill_filter.get(color, set())

            if not openings:
                msg = ("No alternative openings found." if is_alt
                       else f"No openings found in {color} book.")
                tk.Label(inner, text=msg,
                         fg="#888", font=("Arial", 9)).pack(padx=10, pady=10)
            else:
                if is_alt:
                    tk.Label(inner,
                             text="These openings are only reachable via an alternative move.",
                             fg="#1a4a8a", font=("Arial", 8, "italic"),
                             wraplength=380, justify="left").pack(
                                 padx=8, pady=(6, 2), anchor="w")
                for name, count in openings:
                    var = tk.BooleanVar(value=(name in current))
                    check_vars[(color, name)] = var
                    if is_alt:
                        alt_keys.add((color, name))
                    row = tk.Frame(inner)
                    row.pack(fill=tk.X, padx=6, pady=1)
                    cb = tk.Checkbutton(row, variable=var, text=name,
                                        anchor="w", font=("Arial", 9),
                                        fg="#1a4a8a" if is_alt else "white")
                    cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
                    tk.Label(row, text=f"{count} move{'s' if count != 1 else ''}",
                             fg="#888", font=("Arial", 8)).pack(side=tk.RIGHT, padx=4)

        # Single Toplevel-level binding catches MouseWheel from every descendant widget
        # (Tk fires bindings in the widget's bindtag chain, which always includes the Toplevel).
        def _on_win_mousewheel(event):
            try:
                idx = notebook.index(notebook.select())
                c = tab_canvases.get(idx)
                if c:
                    c.yview_scroll(-1 if event.delta > 0 else 1, "units")
            except Exception:
                pass

        win.bind("<MouseWheel>", _on_win_mousewheel)

        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=8)

        def _apply() -> None:
            self._drill_filter = {}
            for (color, name), var in check_vars.items():
                if var.get():
                    self._drill_filter.setdefault(color, set()).add(name)
            # Auto-enable "include alternative moves" if any alt opening is selected
            if any(var.get() for key, var in check_vars.items() if key in alt_keys):
                self._drill_include_alt_var.set(True)
            self._drill_filter_fens = {}
            for color, names in self._drill_filter.items():
                self._drill_filter_fens[color] = compute_filter_fens(
                    names, color, self.db_path
                )
            self._update_filter_label()
            win.destroy()

        def _clear() -> None:
            for var in check_vars.values():
                var.set(False)

        for text, cmd, bg, fg in [
            ("Apply Filter", _apply,    "#2a4a6e", "white"),
            ("Clear All",    _clear,    "#dddddd", "black"),
            ("Cancel",       win.destroy, "#dddddd", "black"),
        ]:
            lbl = tk.Label(btn_frame, text=text, bg=bg, fg=fg,
                           font=("Arial", 9, "bold"), padx=8, pady=4, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=2)
            lbl.bind("<Button-1>", lambda _, c=cmd: c())

    def _check_and_prompt_new_book_move(
        self, from_fen: str, excluded_move_id: int, prev_status: str | None = None
    ) -> None:
        """If no green/yellow move remains at from_fen, prompt the user to set a new one."""
        conn = get_connection(self.db_path)
        try:
            remaining = conn.execute(
                """SELECT id FROM moves
                   WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                     AND repertoire = ? AND is_my_move = 1
                     AND prep_status IN ('green', 'yellow')
                     AND id != ?""",
                (from_fen, self.repertoire, excluded_move_id),
            ).fetchone()
        finally:
            conn.close()
        if remaining is None:
            self._show_new_book_move_dialog(from_fen, excluded_move_id, prev_status)

    def _show_new_book_move_dialog(
        self, from_fen: str, excluded_move_id: int | None = None, prev_status: str | None = None
    ) -> None:
        """Prompt the user to enter a new green/yellow book move from from_fen."""
        try:
            board = chess.Board(from_fen + " 0 1")
        except Exception:
            return

        win = tk.Toplevel(self.root)
        win.title("Set New Book Move")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()

        tk.Label(
            win,
            text="No preferred move at this position",
            font=("Arial", 12, "bold"),
            fg="#8b4500",
        ).pack(padx=16, pady=(14, 2))
        tk.Label(
            win,
            text=(
                "The move that was just labeled off-book was the only green/yellow move "
                "from this position.\n\nEnter the move you intend to play here:"
            ),
            font=("Arial", 10),
            wraplength=380,
            justify="left",
        ).pack(padx=16, pady=(0, 8))

        entry_var = tk.StringVar()
        entry = tk.Entry(win, textvariable=entry_var, font=("Arial", 11), width=14)
        entry.pack(padx=16, pady=(0, 4))
        entry.focus_set()

        label_var = tk.StringVar(value="green")
        radio_frame = tk.Frame(win)
        radio_frame.pack(padx=16, pady=(0, 8), anchor="w")
        tk.Radiobutton(radio_frame, text="Memorized (green)",  variable=label_var, value="green",
                       font=("Arial", 10)).pack(side=tk.LEFT, padx=(0, 12))
        tk.Radiobutton(radio_frame, text="In prep (yellow)", variable=label_var, value="yellow",
                       font=("Arial", 10)).pack(side=tk.LEFT)

        error_var = tk.StringVar()
        tk.Label(win, textvariable=error_var, fg="#c0392b", font=("Arial", 9),
                 wraplength=380).pack(padx=16)

        def _apply() -> None:
            move_str = entry_var.get().strip()
            move: chess.Move | None = None
            try:
                move = board.parse_san(move_str)
            except ValueError:
                try:
                    move = board.parse_uci(move_str)
                except ValueError:
                    pass
            if move is None or move not in board.legal_moves:
                error_var.set("Not a legal move from this position. Try again (e.g. Nf3 or g1f3).")
                return

            uci = move.uci()
            san = board.san(move)
            to_board = board.copy()
            to_board.push(move)
            to_fen = normalize_fen(to_board)
            new_label = label_var.get()

            with transaction(self.db_path) as conn:
                from_pos_row = conn.execute(
                    "SELECT id FROM positions WHERE fen = ?", (from_fen,)
                ).fetchone()
                if from_pos_row is None:
                    error_var.set("Position not found in database.")
                    return
                from_pos_id = from_pos_row["id"]
                to_pos_id = upsert_position(conn, to_fen)
                is_my_move = (board.turn == chess.WHITE) == (self.repertoire == "white")
                move_id = upsert_move(conn, from_pos_id, to_pos_id, uci, san, self.repertoire, is_my_move)
                conn.execute(
                    """UPDATE moves SET prep_status = ?, frequency = (
                           SELECT COALESCE(MAX(frequency), 0) + 1
                           FROM moves WHERE from_position_id = ? AND repertoire = ?
                       ) WHERE id = ?""",
                    (new_label, from_pos_id, self.repertoire, move_id),
                )

            win.destroy()
            if self._drill_mode:
                self._drill_update_notation()
            else:
                self._review_update_notation()

        def _cancel() -> None:
            win.destroy()
            if excluded_move_id is not None and prev_status is not None:
                with transaction(self.db_path) as conn:
                    conn.execute(
                        "UPDATE moves SET prep_status = ? WHERE id = ?",
                        (prev_status, excluded_move_id),
                    )
            if self._drill_mode:
                self._drill_prep_status = prev_status
                self._drill_update_notation()
            else:
                self._review_prep_status = prev_status
                self._review_refresh_prep_buttons()
                self._review_update_notation()

        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=16, pady=(4, 14))
        for text, cmd, bg in [
            ("Confirm", _apply,   "#2a4a6e"),
            ("Cancel",  _cancel,  "#888888"),
        ]:
            lbl = tk.Label(btn_frame, text=text, bg=bg, fg="white",
                           font=("Arial", 9, "bold"), padx=10, pady=6, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=(0, 6))
            lbl.bind("<Button-1>", lambda _, c=cmd: c())

        win.bind("<Return>", lambda _: _apply())

    def _check_and_show_label_conflict(
        self,
        move_id: int,
        from_fen: str,
        labeled_san: str,
        status: str,
    ) -> bool:
        """Check for conflicting green/yellow labels; show dialog if found. Returns True if dialog shown."""
        if status not in ("green", "yellow"):
            return False
        conn = get_connection(self.db_path)
        try:
            conflicts = conn.execute(
                """SELECT m.id, m.uci, m.san, m.prep_status
                   FROM moves m
                   WHERE m.from_position_id = (SELECT id FROM positions WHERE fen = ?)
                     AND m.repertoire = ?
                     AND m.is_my_move = 1
                     AND m.prep_status IN ('green', 'yellow')
                     AND m.id != ?""",
                (from_fen, self.repertoire, move_id),
            ).fetchall()
        finally:
            conn.close()
        if not conflicts:
            return False
        self._show_label_conflict_dialog(
            move_id, from_fen, labeled_san, status, list(conflicts)
        )
        return True

    def _show_label_conflict_dialog(
        self,
        move_id: int,
        from_fen: str,
        labeled_san: str,
        status: str,
        conflicts: list,
    ) -> None:
        conflict_sans = ", ".join(f"'{r['san']}'" for r in conflicts)
        label_str = "Memorized" if status == "green" else "In prep"

        win = tk.Toplevel(self.root)
        win.title("Label Conflict")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()

        tk.Label(
            win,
            text=f"\u26a0  Multiple '{label_str}' moves at this position",
            font=("Arial", 12, "bold"),
            fg="#8b4500",
        ).pack(padx=16, pady=(14, 4))

        msg = (
            f"'{labeled_san}' was just labeled {label_str.lower()}, but "
            f"{conflict_sans} already {'carry' if len(conflicts) > 1 else 'carries'} "
            f"a green or yellow label from this position.\n\n"
            "Only one main prep move per position is recommended. "
            "How would you like to resolve this?"
        )
        tk.Label(win, text=msg, font=("Arial", 10), wraplength=400, justify="left").pack(
            padx=16, pady=(0, 8)
        )

        in_drill = self._drill_mode

        def _make_new_book() -> None:
            with transaction(self.db_path) as conn:
                for r in conflicts:
                    conn.execute("UPDATE moves SET prep_status = 'blue' WHERE id = ?", (r["id"],))
                conn.execute(
                    """UPDATE moves SET frequency = (
                        SELECT COALESCE(MAX(frequency), 0) + 1
                        FROM moves
                        WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                          AND repertoire = ?
                    ) WHERE id = ?""",
                    (from_fen, self.repertoire, move_id),
                )
            win.destroy()
            if in_drill:
                self._drill_update_notation()
                self._drill_load_node()
            else:
                self._review_update_notation()

        def _keep_old_book() -> None:
            with transaction(self.db_path) as conn:
                conn.execute("UPDATE moves SET prep_status = 'blue' WHERE id = ?", (move_id,))
            if in_drill:
                self._drill_prep_status = "blue"
                win.destroy()
                self._drill_update_notation()
                self._drill_load_node()
            else:
                self._review_prep_status = "blue"
                self._review_refresh_prep_buttons()
                win.destroy()
                self._review_update_notation()

        def _keep_new_red_old() -> None:
            with transaction(self.db_path) as conn:
                for r in conflicts:
                    conn.execute("UPDATE moves SET prep_status = 'red' WHERE id = ?", (r["id"],))
                conn.execute(
                    """UPDATE moves SET frequency = (
                        SELECT COALESCE(MAX(frequency), 0) + 1
                        FROM moves
                        WHERE from_position_id = (SELECT id FROM positions WHERE fen = ?)
                          AND repertoire = ?
                    ) WHERE id = ?""",
                    (from_fen, self.repertoire, move_id),
                )
            win.destroy()
            if in_drill:
                self._drill_update_notation()
                self._drill_load_node()
            else:
                self._review_update_notation()

        conflict_label = "conflicts" if len(conflicts) > 1 else f"'{conflicts[0]['san']}'"
        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=16, pady=(4, 14))
        for text, cmd, bg in [
            (f"Make '{labeled_san}' the book move", _make_new_book, "#2a6e2a"),
            (f"Make '{labeled_san}' an alternative", _keep_old_book, "#8b4500"),
            (f"Keep '{labeled_san}', mark {conflict_label} as off-book", _keep_new_red_old, "#6b0000"),
        ]:
            lbl = tk.Label(btn_frame, text=text, bg=bg, fg="white",
                           font=("Arial", 9, "bold"), padx=10, pady=6, cursor="hand2")
            lbl.pack(fill=tk.X, pady=2)
            lbl.bind("<Button-1>", lambda _, c=cmd: c())

    def _drill_set_prep(self, status: str) -> None:
        if self._drill_book_move_id is None:
            return
        prev_status = self._drill_prep_status
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE moves SET prep_status = ? WHERE id = ?",
                (status, self._drill_book_move_id),
            )
        self._drill_prep_status = status
        self._drill_label_frame.pack_forget()
        self._drill_update_notation()
        if status in ("green", "yellow", "red"):
            conn = get_connection(self.db_path)
            try:
                row = conn.execute(
                    """SELECT m.uci, m.san, p.fen AS from_fen
                       FROM moves m JOIN positions p ON p.id = m.from_position_id
                       WHERE m.id = ?""",
                    (self._drill_book_move_id,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                if status == "red":
                    self._check_and_prompt_new_book_move(row["from_fen"], self._drill_book_move_id, prev_status)
                else:
                    had_conflict = self._check_and_show_label_conflict(
                        self._drill_book_move_id, row["from_fen"], row["san"], status
                    )
                    if not had_conflict:
                        self._drill_load_node()
                    return
        self._drill_load_node()

    def _drill_skip_label(self) -> None:
        self._drill_label_frame.pack_forget()
        self._drill_load_node()

    def _drill_update_notation(self) -> None:
        """Rebuild the moves notation text and opening name label."""
        self._drill_opening_var.set(lookup_opening(self.history))

        # Build (from_fen, uci) -> (is_my_move, prep_status) lookup for this repertoire
        move_info: dict[tuple[str, str], tuple[bool, str | None]] = {}
        if self.history:
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    """SELECT p.fen, m.uci, m.is_my_move, m.prep_status
                       FROM moves m
                       JOIN positions p ON p.id = m.from_position_id
                       WHERE m.repertoire = ?""",
                    (self.repertoire,),
                ).fetchall()
                move_info = {
                    (r["fen"], r["uci"]): (bool(r["is_my_move"]), r["prep_status"])
                    for r in rows
                }
            finally:
                conn.close()

        self._drill_notation_txt.config(state=tk.NORMAL)
        self._drill_notation_txt.delete("1.0", tk.END)

        # Format: "  NN.  <white_san:8>  <black_san>\n"
        # Column offsets: white_san starts at 7, black_san starts at 17
        board = chess.Board()
        i = 0
        move_num = 1
        while i < len(self.history):
            white_fen = normalize_fen(board)
            white_uci = self.history[i].uci()
            white_san = board.san(self.history[i])
            board.push(self.history[i])
            i += 1

            black_fen = black_uci = black_san = ""
            if i < len(self.history):
                black_fen = normalize_fen(board)
                black_uci = self.history[i].uci()
                black_san = board.san(self.history[i])
                board.push(self.history[i])
                i += 1

            row_tag = "row_odd" if move_num % 2 == 1 else "row_even"
            line = f"  {move_num:>2}.  {white_san:<8}  {black_san}\n"
            self._drill_notation_txt.insert(tk.END, line, row_tag)

            # Color white san if it's my move and has a prep label
            w_is_my, w_prep = move_info.get((white_fen, white_uci), (False, None))
            if w_is_my and w_prep:
                self._drill_notation_txt.tag_add(
                    f"prep_{w_prep}",
                    f"{move_num}.7",
                    f"{move_num}.{7 + len(white_san)}",
                )

            # Color black san if it's my move and has a prep label
            if black_san:
                b_is_my, b_prep = move_info.get((black_fen, black_uci), (False, None))
                if b_is_my and b_prep:
                    self._drill_notation_txt.tag_add(
                        f"prep_{b_prep}",
                        f"{move_num}.17",
                        f"{move_num}.{17 + len(black_san)}",
                    )

            move_num += 1

        self._drill_notation_txt.config(state=tk.DISABLED)
        self._drill_notation_txt.see(tk.END)

    # ── Engine analysis ──────────────────────────────────────────────────────

    def _analyse_position(self, board: chess.Board | None = None) -> None:
        if self._analysing:
            return
        sf_path = find_stockfish()
        if sf_path is None:
            self._engine_pos_var.set("Stockfish not found — brew install stockfish")
            for var in self._engine_move_vars:
                var.set("")
            return

        board_copy = (board if board is not None else self.board).copy()
        self._analysing = True
        self._analyse_lbl.config(text="Analysing…", bg="#777777", cursor="")
        self._engine_pos_var.set("analysing…")
        for var in self._engine_move_vars:
            var.set("")

        def _run() -> None:
            try:
                with StockfishEngine(sf_path, depth=15) as eng:
                    moves = eng.top_moves(board_copy, n=3)
                self.root.after(0, lambda: self._on_analysis_done(moves, None))
            except Exception as exc:
                self.root.after(0, lambda: self._on_analysis_done(None, str(exc)))

        threading.Thread(target=_run, daemon=True).start()

    def _on_analysis_done(self, moves: list[dict] | None, error: str | None) -> None:
        self._analysing = False
        self._analyse_lbl.config(text="Analyse", bg="#5a4a8a", cursor="hand2")
        if error:
            self._engine_pos_var.set(f"Error: {error[:60]}")
            for var in self._engine_move_vars:
                var.set("")
            return
        if not moves:
            self._engine_pos_var.set("no moves")
            return
        self._show_engine_results(moves)

    def _show_engine_results(self, moves: list[dict]) -> None:
        turn = "W" if self.board.turn == chess.WHITE else "B"
        first = moves[0]
        if first["eval_cp"] is None:
            summary = f"{first['eval_str']} ({turn})"
        else:
            cp = first["eval_cp"]
            if abs(cp) < 20:
                summary = f"{first['eval_str']}  equal"
            elif cp > 0:
                summary = f"{first['eval_str']}  W+"
            else:
                summary = f"{first['eval_str']}  B+"
        self._engine_pos_var.set(summary)

        colors = ["#1a5276", "#333333", "#333333"]
        for i, (var, lbl) in enumerate(zip(self._engine_move_vars, self._engine_move_lbls)):
            if i < len(moves):
                m = moves[i]
                var.set(f"  {i + 1}. {m['san']:<8s}{m['eval_str']}")
                lbl.config(fg=colors[i])
            else:
                var.set("")


def launch_gui(repertoire: str, db_path: Path = DB_PATH, start_drill: bool = False) -> None:
    root = tk.Tk()
    app = RepertoireGUI(root, repertoire, db_path)
    if start_drill:
        root.after(100, app._start_drill)
    root.mainloop()
