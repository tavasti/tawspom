# Tawspom — AI Developer Documentation

Quick reference for AI developers working on the Tawspom codebase.

## Docs Index

| Doc | What it covers |
| ----- | ---------------- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, module layers, data flow, design principles |
| [MODULES.md](MODULES.md) | Every module: role, constructor, key methods/functions |
| [DATABASE.md](DATABASE.md) | Full schema: 8 tables, columns, migrations, access patterns |
| [ALGORITHMS.md](ALGORITHMS.md) | Step-by-step: refill, dedup, discovery, artist radio, storage mapping |
| [COMMANDS.md](COMMANDS.md) | CLI commands with parameters, modes, and behavior |
| [LEGACY_FILES.md](LEGACY_FILES.md) | Unused files and what replaced them |

## Reading Order

1. **ARCHITECTURE.md** — Understand the big picture
2. **MODULES.md** — Know what each file does
3. **DATABASE.md** — Understand data model and access patterns
4. **ALGORITHMS.md** — Deep dive into core logic
5. **COMMANDS.md** — CLI reference for specific tasks
6. **LEGACY_FILES.md** — Avoid working on obsolete code

## Key Facts

- **Entry point**: `main.py` → `tawspom.cli.main()`
- **Business logic**: `tawspom/core/manager.py` (all commands are Manager methods)
- **Spotify API**: `tawspom/core/spotify.py` (retry wrapper + playlist CRUD)
- **Database**: `tawspom/core/db.py` (SQLite, 8 tables, column-presence migrations)
- **Constants**: `tawspom/core/constants.py` (all magic numbers)
- **Models**: `tawspom/models.py` (Track, Transaction, Playlist dataclasses)
- **SPEC.md**: Authoritative product spec — read before making changes
- **AGENTS.md**: AI mandates — read before any session
