#!/usr/bin/env python3
"""Chess Repertoire Manager — command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent / "src"))

from db import DB_PATH, init_db


def cmd_init(args: argparse.Namespace) -> None:
    init_db()
    from drill import init_drill_db
    init_drill_db()
    print("Database initialised at", DB_PATH)


def cmd_import(args: argparse.Namespace) -> None:
    from importer import import_games
    print(f"Importing games for {args.username}...")
    stats = import_games(
        args.username,
        since=args.since,
        max_ply=args.max_ply,
        verbose=True,
    )
    print(stats.summary())
    if stats.errors:
        print("\nErrors:")
        for e in stats.errors[:10]:
            print(f"  {e}")


def cmd_query(args: argparse.Namespace) -> None:
    from query import lookup, lookup_from_pgn, format_node

    if args.pgn:
        node = lookup_from_pgn(args.pgn, args.color)
    else:
        node = lookup(args.moves, args.color)

    if node is None:
        print("Position not found in your repertoire.")
        return

    title = f"Position after: {' '.join(args.moves)}" if args.moves else "Position"
    print(format_node(node, title))


def cmd_tree(args: argparse.Namespace) -> None:
    from query import get_tree
    import json

    tree = get_tree(args.moves, args.color, depth=args.depth)
    if tree is None:
        print("Position not found in your repertoire.")
        return

    def _print_tree(node: dict, indent: int = 0) -> None:
        prefix = "  " * indent
        for m in node.get("moves", []):
            mark = ">" if m["is_my_move"] else " "
            eval_str = f"  eval={m['engine_eval']:+.0f}" if m.get("engine_eval") is not None else ""
            flags = f" [{', '.join(m['flags'])}]" if m.get("flags") else ""
            print(f"{prefix}{mark} {m['san']:8s}  freq={m['frequency']:4d}{eval_str}{flags}")
            if m.get("children"):
                _print_tree({"moves": m["children"]}, indent + 1)

    print(f"Tree (depth {args.depth}, {args.color}):")
    _print_tree(tree)


def cmd_drill(args: argparse.Namespace) -> None:
    if args.stats_only:
        from drill import get_drill_stats
        stats = get_drill_stats(args.color)
        print(f"Due: {stats['due_now']}  |  Reviewed today: {stats['reviewed_today']}  |  "
              f"Correct today: {stats['correct_today']}")
        return

    from gui import launch_gui
    launch_gui(args.color, start_drill=True)


def cmd_review(args: argparse.Namespace) -> None:
    from gui import launch_gui
    launch_gui(args.color)


def cmd_annotate(args: argparse.Namespace) -> None:
    from updater import update_annotation, add_manual_line

    if not args.moves:
        print("Provide a move sequence to annotate.")
        return

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    flags = [f.strip() for f in args.flags.split(",")] if args.flags else None

    ok = update_annotation(
        args.moves, args.color,
        notes=args.notes,
        tags=tags,
        flags=flags,
    )
    if ok:
        print("Annotation updated.")
    else:
        if args.add:
            n = add_manual_line(args.moves, args.color, notes=args.notes or "", tags=tags)
            print(f"Line added ({n} moves).")
        else:
            print("Move not found in DB. Use --add to insert this line manually.")


def cmd_add(args: argparse.Namespace) -> None:
    from updater import add_manual_line, add_manual_pgn

    if args.pgn:
        n = add_manual_line([], args.color, notes=args.notes or "")
        try:
            n = add_manual_pgn(args.pgn, args.color, notes=args.notes or "")
            print(f"Added {n} moves from PGN.")
        except ValueError as e:
            print(f"Error: {e}")
        return

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    n = add_manual_line(args.moves, args.color, notes=args.notes or "", tags=tags)
    print(f"Added/updated {n} moves.")


def cmd_engine(args: argparse.Namespace) -> None:
    from engine import annotate_opening, find_stockfish

    sf = find_stockfish()
    if sf is None:
        print("Stockfish not found. Place binary at data/stockfish/stockfish or install system-wide.")
        return
    print(f"Using Stockfish at: {sf}")
    count = annotate_opening(args.color, depth=args.depth, verbose=True)
    print(f"Annotated {count} moves.")


def cmd_gaps(args: argparse.Namespace) -> None:
    from query import find_coverage_gaps, format_node

    gaps = find_coverage_gaps(args.color)
    if not gaps:
        print("No coverage gaps found.")
        return
    print(f"{len(gaps)} coverage gap(s) in {args.color} repertoire:\n")
    for i, (fen, node) in enumerate(gaps[:args.limit], 1):
        best = node.best_move()
        best_str = f"best: {best.san} (×{best.frequency})" if best else "no moves"
        print(f"  {i}. {fen}")
        print(f"     {best_str}  |  {len(node.my_moves)} candidate(s)")


def cmd_stats(args: argparse.Namespace) -> None:
    from query import get_stats
    from drill import get_drill_stats

    rep_stats = get_stats(args.color)
    drill = get_drill_stats(args.color)
    print(f"=== {args.color.capitalize()} Repertoire ===")
    print(f"  Games imported   : {rep_stats['total_games']}")
    print(f"  Positions        : {rep_stats['total_positions']}")
    print(f"  Moves (total)    : {rep_stats['total_moves']}")
    print(f"  My moves         : {rep_stats['my_moves']}")
    print(f"    Memorized      : {rep_stats['label_green']}")
    print(f"    In prep        : {rep_stats['label_yellow']}")
    print(f"    Off-book       : {rep_stats['label_red']}")
    print(f"    Alternative    : {rep_stats['label_blue']}")
    print(f"    Unlabeled      : {rep_stats['label_none']}")
    print(f"  Coverage gaps    : {rep_stats['coverage_gaps']}")
    print(f"  Drill due now    : {drill['due_now']}")
    print(f"  Reviewed today   : {drill['reviewed_today']}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chess-rep",
        description="Chess Repertoire Manager",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialise the database")

    # import
    pi = sub.add_parser("import", help="Import games from Chess.com")
    pi.add_argument("username", help="Chess.com username")
    pi.add_argument("--since", default=None,
                    help="Only import games after this ISO date (auto-detected if omitted)")
    pi.add_argument("--max-ply", type=int, default=20, dest="max_ply",
                    help="Number of opening plies to record (default 20)")

    # query
    pq = sub.add_parser("query", help="Look up what you play from a position")
    pq.add_argument("moves", nargs="*", help="Move sequence in SAN or UCI")
    pq.add_argument("--color", default="white", choices=["white", "black"])
    pq.add_argument("--pgn", default=None, help="PGN snippet instead of moves")

    # tree
    pt = sub.add_parser("tree", help="Print opening tree from a position")
    pt.add_argument("moves", nargs="*", help="Starting position (SAN or UCI)")
    pt.add_argument("--color", default="white", choices=["white", "black"])
    pt.add_argument("--depth", type=int, default=3)

    # drill
    pd = sub.add_parser("drill", help="Flashcard study session")
    pd.add_argument("--color", default="white", choices=["white", "black"])
    pd.add_argument("--size", type=int, default=20, help="Positions per session")
    pd.add_argument("--stats", dest="stats_only", action="store_true",
                    help="Show drill stats only, don't start a session")

    # review (GUI)
    pr = sub.add_parser("review", help="Open interactive board (requires display)")
    pr.add_argument("--color", default="white", choices=["white", "black"])

    # annotate
    pan = sub.add_parser("annotate", help="Add notes/tags/flags to a move")
    pan.add_argument("moves", nargs="+", help="Move sequence; last move gets annotated")
    pan.add_argument("--color", default="white", choices=["white", "black"])
    pan.add_argument("--notes", default=None)
    pan.add_argument("--tags", default=None, help="Comma-separated tags")
    pan.add_argument("--flags", default=None, help="Comma-separated flags")
    pan.add_argument("--add", action="store_true", help="Add line if not already in DB")

    # add
    padd = sub.add_parser("add", help="Manually add a line to the repertoire")
    padd.add_argument("moves", nargs="*", help="Move sequence (SAN or UCI)")
    padd.add_argument("--color", default="white", choices=["white", "black"])
    padd.add_argument("--notes", default=None)
    padd.add_argument("--tags", default=None, help="Comma-separated tags")
    padd.add_argument("--pgn", default=None, help="PGN string to add instead of moves")

    # engine
    peng = sub.add_parser("engine", help="Run Stockfish annotations on unannotated moves")
    peng.add_argument("--color", default="white", choices=["white", "black"])
    peng.add_argument("--depth", type=int, default=15)

    # gaps
    pg = sub.add_parser("gaps", help="List coverage gaps")
    pg.add_argument("--color", default="white", choices=["white", "black"])
    pg.add_argument("--limit", type=int, default=20)

    # stats
    ps = sub.add_parser("stats", help="Show repertoire statistics")
    ps.add_argument("--color", default="white", choices=["white", "black"])

    return p


_COMMANDS = {
    "init": cmd_init,
    "import": cmd_import,
    "query": cmd_query,
    "tree": cmd_tree,
    "drill": cmd_drill,
    "review": cmd_review,
    "annotate": cmd_annotate,
    "add": cmd_add,
    "engine": cmd_engine,
    "gaps": cmd_gaps,
    "stats": cmd_stats,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
