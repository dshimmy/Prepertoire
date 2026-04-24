"""Opening name lookup via ECO position matching."""

from __future__ import annotations

import chess

from tree import normalize_fen

# Each entry: (san_move_sequence, opening_name).
# Ordered general → specific so later (more specific) entries override earlier ones
# for the same terminal position.  The lookup returns the LAST match found while
# replaying the game, so the deepest matching entry wins.
_RAW: list[tuple[str, str]] = [
    # ── Starting position ───────────────────────────────────────────────────
    ("", "Starting Position"),

    # ── 1.e4 ────────────────────────────────────────────────────────────────
    ("e4", "King's Pawn Opening"),
    # King's Gambit
    ("e4 e5 f4", "King's Gambit"),
    ("e4 e5 f4 exf4", "King's Gambit Accepted"),
    ("e4 e5 f4 Bc5", "King's Gambit Declined"),
    ("e4 e5 f4 d5", "Falkbeer Counter-Gambit"),
    # Open game
    ("e4 e5", "Open Game"),
    ("e4 e5 Nf3", "King's Knight Opening"),
    ("e4 e5 Nf3 Nc6", "Three Knights Game"),
    ("e4 e5 Nf3 Nc6 Nc3", "Four Knights Game"),
    ("e4 e5 Nf3 Nc6 Nc3 Nf6", "Four Knights Game"),
    ("e4 e5 Nf3 Nc6 Nc3 Bb4", "Four Knights, Spanish Variation"),
    # Ruy Lopez
    ("e4 e5 Nf3 Nc6 Bb5", "Ruy Lopez"),
    ("e4 e5 Nf3 Nc6 Bb5 a6", "Ruy Lopez, Morphy Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4", "Ruy Lopez, Morphy Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6", "Ruy Lopez, Morphy Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O", "Ruy Lopez, Closed"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7", "Ruy Lopez, Closed"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O b5", "Ruy Lopez, Archangel"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Nxe4", "Ruy Lopez, Open Variation"),
    ("e4 e5 Nf3 Nc6 Bb5 a6 Bxc6", "Ruy Lopez, Exchange Variation"),
    ("e4 e5 Nf3 Nc6 Bb5 Nf6", "Ruy Lopez, Berlin Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 Nf6 O-O Nxe4", "Ruy Lopez, Berlin Defense, Rio Gambit"),
    ("e4 e5 Nf3 Nc6 Bb5 Nf6 O-O Nxe4 d4", "Ruy Lopez, Berlin Endgame"),
    ("e4 e5 Nf3 Nc6 Bb5 d6", "Ruy Lopez, Steinitz Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 Bc5", "Ruy Lopez, Classical Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 g6", "Ruy Lopez, Smyslov Defense"),
    ("e4 e5 Nf3 Nc6 Bb5 f5", "Ruy Lopez, Schliemann Defense"),
    # Italian
    ("e4 e5 Nf3 Nc6 Bc4", "Italian Game"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5", "Italian Game, Giuoco Piano"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 c3", "Italian Game, Giuoco Piano"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6", "Italian Game, Giuoco Piano"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d4", "Italian Game, Greco Attack"),
    ("e4 e5 Nf3 Nc6 Bc4 Nf6", "Italian Game, Two Knights Defense"),
    ("e4 e5 Nf3 Nc6 Bc4 Nf6 Ng5", "Italian Game, Two Knights, Fried Liver Attack"),
    ("e4 e5 Nf3 Nc6 Bc4 Nf6 d3", "Italian Game, London System"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 O-O Nf6", "Italian Game, Modern"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 O-O Nf6 d3", "Italian Game, Modern"),
    # Scotch
    ("e4 e5 Nf3 Nc6 d4", "Scotch Game"),
    ("e4 e5 Nf3 Nc6 d4 exd4", "Scotch Game"),
    ("e4 e5 Nf3 Nc6 d4 exd4 Nxd4", "Scotch Game"),
    ("e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Nf6", "Scotch Game, Schmidt Variation"),
    ("e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Bc5", "Scotch Game, Classical Variation"),
    ("e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Qh4", "Scotch Game, Haxo Gambit"),
    ("e4 e5 d4", "Center Game"),
    ("e4 e5 d4 exd4", "Center Game"),
    ("e4 e5 Nf3 d6", "Philidor Defense"),
    ("e4 e5 Nf3 f5", "Latvian Gambit"),
    ("e4 e5 Nc3", "Vienna Game"),
    ("e4 e5 Nc3 Nc6", "Vienna Game"),
    ("e4 e5 Nc3 Nf6", "Vienna Game, Falkbeer Variation"),
    ("e4 e5 Nc3 Bc5", "Vienna Game, Max Lange Defense"),
    # Sicilian
    ("e4 c5", "Sicilian Defense"),
    ("e4 c5 Nf3", "Sicilian Defense"),
    ("e4 c5 Nf3 d6", "Sicilian Defense, Najdorf Variation"),
    ("e4 c5 Nf3 d6 d4", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 d6 d4 cxd4", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6", "Sicilian Defense, Najdorf Variation"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Bg5", "Sicilian Najdorf, English Attack"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be3", "Sicilian Najdorf, English Attack"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be2", "Sicilian Najdorf, Classical Variation"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 f3", "Sicilian Najdorf, English Attack"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6", "Sicilian Dragon"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6 Be3", "Sicilian Dragon, Yugoslav Attack"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6 Be2", "Sicilian Dragon, Classical Variation"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 e6", "Sicilian Defense, Scheveningen Variation"),
    ("e4 c5 Nf3 Nc6", "Sicilian Defense"),
    ("e4 c5 Nf3 Nc6 d4", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 Nc6 d4 cxd4 Nxd4", "Sicilian Defense, Open"),
    ("e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 Nf6", "Sicilian Defense, Classical Variation"),
    ("e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 e6", "Sicilian Defense, Taimanov Variation"),
    ("e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 g6", "Sicilian Defense, Accelerated Dragon"),
    ("e4 c5 Nf3 e6", "Sicilian Defense"),
    ("e4 c5 Nf3 e6 d4 cxd4 Nxd4", "Sicilian Defense, Kan/Taimanov"),
    ("e4 c5 Nf3 e6 d4 cxd4 Nxd4 a6", "Sicilian Defense, Kan Variation"),
    ("e4 c5 Nf3 e6 d4 cxd4 Nxd4 Nc6", "Sicilian Defense, Taimanov Variation"),
    ("e4 c5 c3", "Sicilian Defense, Alapin Variation"),
    ("e4 c5 c3 d5", "Sicilian Alapin, d5 variation"),
    ("e4 c5 c3 Nf6", "Sicilian Alapin, Nf6 variation"),
    ("e4 c5 Nc3", "Sicilian Defense, Closed"),
    ("e4 c5 Nc3 Nc6", "Sicilian Defense, Closed"),
    ("e4 c5 Nc3 e6", "Sicilian Defense, Closed"),
    ("e4 c5 d4", "Sicilian Defense, Smith-Morra Gambit"),
    ("e4 c5 d4 cxd4 c3", "Sicilian Defense, Smith-Morra Gambit"),
    # French
    ("e4 e6", "French Defense"),
    ("e4 e6 d4 d5", "French Defense"),
    ("e4 e6 d4 d5 e5", "French Defense, Advance Variation"),
    ("e4 e6 d4 d5 e5 c5", "French Defense, Advance, Wade Variation"),
    ("e4 e6 d4 d5 exd5", "French Defense, Exchange Variation"),
    ("e4 e6 d4 d5 Nc3", "French Defense, Classical Variation"),
    ("e4 e6 d4 d5 Nc3 Nf6", "French Defense, Classical Variation"),
    ("e4 e6 d4 d5 Nc3 Bb4", "French Defense, Winawer Variation"),
    ("e4 e6 d4 d5 Nc3 Bb4 e5", "French Winawer, Advance Variation"),
    ("e4 e6 d4 d5 Nc3 Bb4 exd5", "French Winawer, Exchange"),
    ("e4 e6 d4 d5 Nd2", "French Defense, Tarrasch Variation"),
    ("e4 e6 d4 d5 Nd2 Nf6", "French Defense, Tarrasch, Open Variation"),
    ("e4 e6 d4 d5 Nd2 c5", "French Defense, Tarrasch, Open Variation"),
    ("e4 e6 d4 d5 Nd2 Nc6", "French Defense, Tarrasch, Guimard Variation"),
    # Caro-Kann
    ("e4 c6", "Caro-Kann Defense"),
    ("e4 c6 d4 d5", "Caro-Kann Defense"),
    ("e4 c6 d4 d5 e5", "Caro-Kann, Advance Variation"),
    ("e4 c6 d4 d5 e5 Bf5", "Caro-Kann, Advance, Short Variation"),
    ("e4 c6 d4 d5 exd5 cxd5", "Caro-Kann, Exchange Variation"),
    ("e4 c6 d4 d5 Nc3 dxe4 Nxe4", "Caro-Kann, Classical Variation"),
    ("e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5", "Caro-Kann, Classical Variation"),
    ("e4 c6 d4 d5 Nc3 dxe4 Nxe4 Nd7", "Caro-Kann, Karpov Variation"),
    ("e4 c6 d4 d5 Nd2 dxe4 Nxe4", "Caro-Kann, Kasparian Variation"),
    ("e4 c6 d4 d5 Nc3 g6", "Caro-Kann, Gurgenidze System"),
    # Pirc / Modern
    ("e4 d6", "Pirc Defense"),
    ("e4 d6 d4 Nf6", "Pirc Defense"),
    ("e4 d6 d4 Nf6 Nc3", "Pirc Defense"),
    ("e4 d6 d4 Nf6 Nc3 g6", "Pirc Defense, Classical"),
    ("e4 d6 d4 Nf6 Nc3 g6 Be3", "Pirc Defense, Austrian Attack"),
    ("e4 d6 d4 Nf6 Nc3 g6 f4", "Pirc Defense, Austrian Attack"),
    ("e4 g6", "Modern Defense"),
    ("e4 g6 d4 d6", "Modern Defense"),
    ("e4 g6 d4 Bg7", "Modern Defense"),
    # Scandinavian
    ("e4 d5", "Scandinavian Defense"),
    ("e4 d5 exd5", "Scandinavian Defense"),
    ("e4 d5 exd5 Qxd5", "Scandinavian Defense, Main Line"),
    ("e4 d5 exd5 Qxd5 Nc3", "Scandinavian Defense, Main Line"),
    ("e4 d5 exd5 Qxd5 Nc3 Qa5", "Scandinavian Defense, Mieses-Kotroc Variation"),
    ("e4 d5 exd5 Nf6", "Scandinavian Defense, Modern Variation"),
    # Alekhine
    ("e4 Nf6", "Alekhine's Defense"),
    ("e4 Nf6 e5 Nd5", "Alekhine's Defense"),
    ("e4 Nf6 e5 Nd5 d4 d6", "Alekhine's Defense, Modern Variation"),
    ("e4 Nf6 e5 Nd5 d4 d6 c4 Nb6", "Alekhine's Defense, Modern Variation"),
    # Dutch / other 1.e4
    ("e4 Nc6", "Nimzowitsch Defense"),
    ("e4 b6", "Owen's Defense"),

    # ── 1.d4 ────────────────────────────────────────────────────────────────
    ("d4", "Queen's Pawn Opening"),
    ("d4 d5", "Queen's Pawn Game"),
    # Queen's Gambit
    ("d4 d5 c4", "Queen's Gambit"),
    ("d4 d5 c4 e6", "Queen's Gambit Declined"),
    ("d4 d5 c4 e6 Nc3", "Queen's Gambit Declined"),
    ("d4 d5 c4 e6 Nc3 Nf6", "Queen's Gambit Declined"),
    ("d4 d5 c4 e6 Nc3 Nf6 Bg5", "Queen's Gambit Declined, Main Line"),
    ("d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7", "Queen's Gambit Declined, Main Line"),
    ("d4 d5 c4 e6 Nc3 Nf6 Bg5 h6", "Queen's Gambit Declined, Manhattan Variation"),
    ("d4 d5 c4 e6 Nf3 Nf6", "Queen's Gambit Declined"),
    ("d4 d5 c4 e6 Nf3 Nf6 Nc3 Be7", "Queen's Gambit Declined, Lasker Defense"),
    ("d4 d5 c4 e6 Nf3 Nf6 Nc3 c6", "Semi-Slav Defense"),
    ("d4 d5 c4 e6 Nf3 Nf6 Nc3 c6 Bg5", "Semi-Slav, Anti-Moscow Gambit"),
    ("d4 d5 c4 e6 Nf3 Nf6 Nc3 c6 e3", "Semi-Slav, Meran Variation"),
    ("d4 d5 c4 e6 Nf3 Nf6 Nc3 Bb4", "Ragozin Defense"),
    ("d4 d5 c4 e6 Nf3 Nf6 Nc3 dxc4", "Queen's Gambit Accepted, modern"),
    # Slav
    ("d4 d5 c4 c6", "Slav Defense"),
    ("d4 d5 c4 c6 Nf3 Nf6", "Slav Defense"),
    ("d4 d5 c4 c6 Nf3 Nf6 Nc3", "Slav Defense"),
    ("d4 d5 c4 c6 Nf3 Nf6 Nc3 dxc4", "Slav Defense, Accepted"),
    ("d4 d5 c4 c6 Nf3 Nf6 Nc3 e6", "Semi-Slav Defense"),
    ("d4 d5 c4 c6 Nc3 Nf6 e3", "Slav Defense, Exchange Variation"),
    # QGA
    ("d4 d5 c4 dxc4", "Queen's Gambit Accepted"),
    ("d4 d5 c4 dxc4 Nf3", "Queen's Gambit Accepted"),
    ("d4 d5 c4 dxc4 Nf3 Nf6", "Queen's Gambit Accepted"),
    ("d4 d5 c4 dxc4 e3", "Queen's Gambit Accepted, Classical"),
    # London / Colle
    ("d4 d5 Nf3", "London System"),
    ("d4 d5 Nf3 Nf6 Bf4", "London System"),
    ("d4 d5 Nf3 Nf6 e3", "Colle System"),
    ("d4 d5 Nf3 Nf6 c4", "Queen's Gambit"),
    # Nimzo-Indian
    ("d4 Nf6 c4 e6 Nc3 Bb4", "Nimzo-Indian Defense"),
    ("d4 Nf6 c4 e6 Nc3 Bb4 e3", "Nimzo-Indian, Rubinstein System"),
    ("d4 Nf6 c4 e6 Nc3 Bb4 Qc2", "Nimzo-Indian, Classical Variation"),
    ("d4 Nf6 c4 e6 Nc3 Bb4 a3", "Nimzo-Indian, Sämisch Variation"),
    ("d4 Nf6 c4 e6 Nc3 Bb4 Nf3", "Nimzo-Indian, Three Knights Variation"),
    ("d4 Nf6 c4 e6 Nc3 Bb4 f3", "Nimzo-Indian, Sämisch Attack"),
    # Queen's Indian
    ("d4 Nf6 c4 e6 Nf3 b6", "Queen's Indian Defense"),
    ("d4 Nf6 c4 e6 Nf3 b6 g3", "Queen's Indian, Fianchetto Variation"),
    ("d4 Nf6 c4 e6 Nf3 b6 Nc3", "Queen's Indian, Classical Variation"),
    ("d4 Nf6 c4 e6 Nf3 b6 e3", "Queen's Indian, Petrosian System"),
    # King's Indian
    ("d4 Nf6 c4 g6", "King's Indian Defense"),
    ("d4 Nf6 c4 g6 Nc3 Bg7", "King's Indian Defense"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4", "King's Indian Defense"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6", "King's Indian Defense"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3", "King's Indian Defense, Classical Variation"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Be2", "King's Indian Defense, Classical Variation"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Be2 O-O", "King's Indian Defense, Classical Variation"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Be2 O-O Nf3", "King's Indian Defense, Classical Variation"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Be2 O-O Nf3 e5", "King's Indian Defense, Classical Main Line"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 f3", "King's Indian Defense, Sämisch Variation"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Bg5", "King's Indian Defense, Averbakh Variation"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3 O-O Bg5", "King's Indian Defense, Averbakh System"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 O-O", "King's Indian Defense"),
    # Grünfeld
    ("d4 Nf6 c4 g6 Nc3 d5", "Grünfeld Defense"),
    ("d4 Nf6 c4 g6 Nc3 d5 cxd5 Nxd5", "Grünfeld Defense"),
    ("d4 Nf6 c4 g6 Nc3 d5 cxd5 Nxd5 e4", "Grünfeld Defense, Exchange Variation"),
    ("d4 Nf6 c4 g6 Nc3 d5 cxd5 Nxd5 e4 Nxc3", "Grünfeld Defense, Exchange Variation"),
    ("d4 Nf6 c4 g6 Nc3 d5 Nf3", "Grünfeld Defense, Three Knights Variation"),
    ("d4 Nf6 c4 g6 Nf3 Bg7 Nc3 d5", "Grünfeld Defense"),
    # Benoni
    ("d4 Nf6 c4 c5 d5", "Benoni Defense"),
    ("d4 Nf6 c4 c5 d5 e6", "Benoni Defense, Modern Variation"),
    ("d4 Nf6 c4 c5 d5 e6 Nc3 exd5 cxd5", "Benoni Defense, Modern Variation"),
    ("d4 c5", "Benoni Defense"),
    ("d4 c5 d5", "Benoni Defense"),
    # Dutch
    ("d4 f5", "Dutch Defense"),
    ("d4 f5 Nf3", "Dutch Defense"),
    ("d4 f5 c4 Nf6", "Dutch Defense"),
    ("d4 f5 c4 Nf6 Nc3 e6", "Dutch Defense, Classical"),
    ("d4 f5 c4 Nf6 g3 g6", "Dutch Defense, Leningrad Variation"),
    ("d4 f5 c4 e6 Nc3 Nf6 g3 d5", "Dutch Defense, Stonewall Variation"),
    # Trompowsky / London
    ("d4 Nf6 Bg5", "Trompowsky Attack"),
    ("d4 Nf6 Nf3", "Indian Game"),
    ("d4 Nf6 Nf3 g6", "Indian Game, King's Indian formation"),
    ("d4 Nf6 Nf3 e6", "Indian Game"),
    ("d4 Nf6 Nf3 d5 Bf4", "London System"),
    ("d4 d5 Bf4", "London System"),
    ("d4 d5 Bf4 Nf6", "London System"),
    ("d4 d5 Bf4 Nf6 e3", "London System"),
    ("d4 d5 Bf4 e6", "London System"),
    ("d4 d5 Bf4 c5", "London System"),
    # Catalan
    ("d4 Nf6 c4 e6 Nf3 d5 g3", "Catalan Opening"),
    ("d4 Nf6 c4 e6 Nf3 d5 g3 Bb4+", "Catalan Opening"),
    ("d4 Nf6 c4 e6 Nf3 d5 g3 Be7", "Catalan Opening, Closed"),
    ("d4 Nf6 c4 e6 Nf3 d5 g3 dxc4", "Catalan Opening, Open"),
    ("d4 Nf6 c4 e6 g3", "Catalan Opening"),

    # ── 1.c4 ────────────────────────────────────────────────────────────────
    ("c4", "English Opening"),
    ("c4 e5", "English Opening, Reversed Sicilian"),
    ("c4 e5 Nf3", "English Opening, Reversed Sicilian"),
    ("c4 e5 Nc3", "English Opening, Reversed Sicilian"),
    ("c4 e5 Nc3 Nf6", "English Opening, Four Knights"),
    ("c4 c5", "English Opening, Symmetrical Variation"),
    ("c4 c5 Nf3", "English Opening, Symmetrical"),
    ("c4 c5 Nc3 Nf6", "English Opening, Symmetrical"),
    ("c4 c5 Nc3 Nc6", "English Opening, Symmetrical"),
    ("c4 Nf6", "English Opening, Anglo-Indian Defense"),
    ("c4 Nf6 Nc3 d5", "English Opening, Anglo-Grünfeld"),
    ("c4 Nf6 Nc3 e6", "English Opening, Anglo-Indian"),
    ("c4 Nf6 Nc3 g6", "English Opening, King's Indian Formation"),
    ("c4 e6", "English Opening, Agincourt Defense"),
    ("c4 g6", "English Opening, King's Indian Formation"),

    # ── 1.Nf3 ───────────────────────────────────────────────────────────────
    ("Nf3", "Reti Opening"),
    ("Nf3 d5", "Reti Opening"),
    ("Nf3 d5 c4", "Reti Opening, Main Line"),
    ("Nf3 d5 g3", "Reti Opening, King's Indian Attack"),
    ("Nf3 Nf6", "Reti Opening"),
    ("Nf3 Nf6 c4", "English Opening"),
    ("Nf3 c5", "Reti Opening"),
    ("Nf3 d5 c4 d4", "Reti Gambit"),

    # ── 1.g3 / 1.b3 / 1.f4 ─────────────────────────────────────────────────
    ("g3", "King's Fianchetto Opening"),
    ("g3 d5 Bg2", "King's Fianchetto Opening"),
    ("b3", "Nimzowitsch-Larsen Attack"),
    ("b3 e5", "Nimzowitsch-Larsen Attack"),
    ("b3 d5", "Nimzowitsch-Larsen Attack"),
    ("f4", "Bird's Opening"),
    ("f4 e5", "Bird's Opening, From's Gambit"),
    ("f4 d5", "Bird's Opening"),
    ("f4 Nf6", "Bird's Opening"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 b4", "Evans Gambit"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5 b4 Bxb4", "Evans Gambit Accepted"),
]

# Build EPD → opening name lookup (later/more-specific entries override earlier ones)
_LOOKUP: dict[str, str] = {}

def _build_lookup() -> None:
    for san_str, name in _RAW:
        board = chess.Board()
        key = normalize_fen(board)
        if not san_str:
            _LOOKUP[key] = name
            continue
        try:
            for token in san_str.split():
                board.push_san(token)
        except ValueError:
            continue
        _LOOKUP[normalize_fen(board)] = name

_build_lookup()


def lookup_opening(history: list[chess.Move]) -> str:
    """Return the most specific ECO opening name matching the given move history."""
    board = chess.Board()
    found = ""
    for move in history:
        board.push(move)
        name = _LOOKUP.get(normalize_fen(board))
        if name:
            found = name
    return found


# ── Opening filter helpers ────────────────────────────────────────────────────

# Map each unique opening name to the shallowest (first-in-_RAW) FEN for that name.
# Used for the drill opening filter.
_NAME_TO_FEN: dict[str, str] = {}


def _build_name_to_fen() -> None:
    for san_str, name in _RAW:
        if name in _NAME_TO_FEN or not name or name == "Starting Position":
            continue
        board = chess.Board()
        if san_str:
            try:
                for token in san_str.split():
                    board.push_san(token)
            except ValueError:
                continue
        _NAME_TO_FEN[name] = normalize_fen(board)


_build_name_to_fen()


# (green, yellow, red, blue, unlabeled)
LabelCounts = tuple[int, int, int, int, int]


def _count_moves_by_label(conn, fen: str, repertoire: str) -> LabelCounts:
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(pos_id) AS (
            SELECT id FROM positions WHERE fen = ?
            UNION
            SELECT m.to_position_id
            FROM moves m JOIN subtree s ON m.from_position_id = s.pos_id
            WHERE m.repertoire = ?
        )
        SELECT prep_status, COUNT(*) AS cnt
        FROM moves m2
        JOIN subtree s ON m2.from_position_id = s.pos_id
        WHERE m2.repertoire = ? AND m2.is_my_move = 1
        GROUP BY prep_status
        """,
        (fen, repertoire, repertoire),
    ).fetchall()
    counts: dict[str | None, int] = {r["prep_status"]: r["cnt"] for r in rows}
    return (
        counts.get("green", 0),
        counts.get("yellow", 0),
        counts.get("red", 0),
        counts.get("blue", 0),
        counts.get(None, 0),
    )


_START_FEN = normalize_fen(chess.Board())


def get_openings_split(
    repertoire: str, db_path
) -> tuple[list[tuple[str, LabelCounts]], list[tuple[str, LabelCounts]]]:
    """Return (main_openings, alt_openings) each as sorted (name, label_counts) lists.

    Main: reachable from the starting position without passing through a
          blue-labeled (alternative) my-move.
    Alt:  only reachable via at least one blue-labeled my-move.
    label_counts is (green, yellow, red, blue, unlabeled).
    """
    from db import get_connection

    conn = get_connection(db_path)
    try:
        # Positions reachable without ever traversing a blue-labeled my-move
        non_alt_rows = conn.execute(
            """
            WITH RECURSIVE non_alt(pos_id) AS (
                SELECT id FROM positions WHERE fen = ?
                UNION
                SELECT m.to_position_id
                FROM moves m
                JOIN non_alt n ON n.pos_id = m.from_position_id
                WHERE m.repertoire = ?
                  AND NOT (m.is_my_move = 1 AND m.prep_status = 'blue')
            )
            SELECT p.fen FROM positions p JOIN non_alt n ON p.id = n.pos_id
            """,
            (_START_FEN, repertoire),
        ).fetchall()
        non_alt_fens = {r["fen"] for r in non_alt_rows}

        fen_rows = conn.execute(
            "SELECT DISTINCT p.fen FROM positions p "
            "JOIN moves m ON p.id = m.from_position_id WHERE m.repertoire = ?",
            (repertoire,),
        ).fetchall()
        fen_set = {r["fen"] for r in fen_rows}

        main: list[tuple[str, LabelCounts]] = []
        alt: list[tuple[str, LabelCounts]] = []
        for name, fen in _NAME_TO_FEN.items():
            if fen not in fen_set:
                continue
            lc = _count_moves_by_label(conn, fen, repertoire)
            if sum(lc) == 0:
                continue
            (main if fen in non_alt_fens else alt).append((name, lc))

        return sorted(main, key=lambda x: x[0]), sorted(alt, key=lambda x: x[0])
    finally:
        conn.close()


def compute_filter_fens(opening_names: set[str], repertoire: str, db_path) -> set[str]:
    """Compute valid FENs for the given opening filter.

    Returns ancestors (path from start to opening) + opening FEN + all descendants.
    Only edges from the given repertoire are considered.
    """
    from db import get_connection

    opening_fens = {_NAME_TO_FEN[n] for n in opening_names if n in _NAME_TO_FEN}
    if not opening_fens:
        return set()

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT pf.fen AS from_fen, pt.fen AS to_fen
            FROM moves m
            JOIN positions pf ON pf.id = m.from_position_id
            JOIN positions pt ON pt.id = m.to_position_id
            WHERE m.repertoire = ?
            """,
            (repertoire,),
        ).fetchall()
    finally:
        conn.close()

    forward: dict[str, list[str]] = {}
    backward: dict[str, list[str]] = {}
    for r in rows:
        forward.setdefault(r["from_fen"], []).append(r["to_fen"])
        backward.setdefault(r["to_fen"], []).append(r["from_fen"])

    valid: set[str] = set()
    for fen in opening_fens:
        valid.add(fen)
        # BFS backward: all ancestor positions on the path from start to this opening
        queue = [fen]
        visited: set[str] = {fen}
        while queue:
            cur = queue.pop(0)
            for prev in backward.get(cur, []):
                if prev not in visited:
                    visited.add(prev)
                    valid.add(prev)
                    queue.append(prev)
        # BFS forward: all descendant positions reachable from this opening
        queue = [fen]
        visited2: set[str] = {fen}
        while queue:
            cur = queue.pop(0)
            for nxt in forward.get(cur, []):
                if nxt not in visited2:
                    visited2.add(nxt)
                    valid.add(nxt)
                    queue.append(nxt)

    return valid
