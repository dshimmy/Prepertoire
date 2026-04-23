# Prepertoire

A personal chess opening repertoire manager. Import your Chess.com games, explore what you actually play, drill positions with spaced repetition, and annotate lines with Stockfish evaluations — all from the command line or a lightweight Tkinter board.

## AI Notice

Claude Code was used to implement nearly all the features in this project. 

## Features

- **Import** — pull your Chess.com game history and extract opening moves into a local SQLite database
- **Query / Tree** — look up what you play from any position, or print a branching tree of your repertoire
- **Drill** — spaced-repetition flashcard sessions: the board shows a position and you guess the next move
- **Review** — interactive board GUI for browsing and editing your repertoire
- **Engine** — run Stockfish on unannotated positions to add centipawn evaluations
- **Annotate / Add** — attach notes, tags, and flags to moves, or manually add lines
- **Gaps** — find positions where you have no clear repertoire choice
- **Stats** — summary counts for games, positions, moves, and drill progress

## Requirements

- Python 3.12+
- [python-chess](https://python-chess.readthedocs.io/) and [requests](https://requests.readthedocs.io/) (see `requirements.txt`)
- Stockfish binary for engine features (optional)

```
pip install -r requirements.txt
```

**Stockfish** (optional):
```
# macOS
brew install stockfish

# Ubuntu/Debian
sudo apt install stockfish

# or place the binary at data/stockfish/stockfish
```

## Quick start

```bash
# 1. Initialise the database
python main.py init

# 2. Import your Chess.com games
python main.py import YOUR_USERNAME

# 3. See what you play after 1. d4
python main.py query d4

# 4. Print a tree 3 moves deep
python main.py tree e4 --depth 3

# 5. Start a drill session
python main.py drill

# 6. Open the interactive board
python main.py review
```

## Commands

| Command | Description |
|---|---|
| `init` | Initialise the local database |
| `import <username>` | Import games from Chess.com |
| `query [moves...]` | Look up repertoire at a position |
| `tree [moves...]` | Print branching opening tree |
| `drill` | Spaced-repetition study session |
| `review` | Interactive board GUI |
| `annotate <moves...>` | Add notes, tags, or flags to a move |
| `add [moves...]` | Manually insert a line |
| `engine` | Annotate positions with Stockfish |
| `gaps` | List positions with no clear repertoire choice |
| `stats` | Show repertoire and drill statistics |

Run `python main.py <command> --help` for full options on any command.

## Project layout

```
main.py          # CLI entry point
src/
  db.py          # SQLite schema and connection helpers
  importer.py    # Chess.com API fetcher
  query.py       # Repertoire lookups and tree building
  tree.py        # Move/position node data structures
  drill.py       # Spaced-repetition logic
  engine.py      # Stockfish integration
  gui.py         # Tkinter board UI
  updater.py     # Write-path helpers (annotate, add lines)
  openings.py    # ECO opening name lookups
  pieces.py      # Piece image loader
data/
  repertoire.db  # SQLite database (created on first init)
```
