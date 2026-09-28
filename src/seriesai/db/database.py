from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import DDL, MIGRATIONS, SCHEMA_VERSION


class Database:
    """SQLite access with WAL mode and transactional schema initialization."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, backup_directory: Path | str | None = None) -> None:
        previous_version = self.schema_version() if self.path.exists() else 0
        if previous_version and previous_version < SCHEMA_VERSION and backup_directory:
            self.backup_to(backup_directory)
        with self.connect() as connection:
            connection.executescript(DDL)
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
            for version in range(2, SCHEMA_VERSION + 1):
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
                    continue
                connection.executescript(MIGRATIONS[version])
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))

    def schema_version(self) -> int:
        if not self.path.exists():
            return 0
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] or 0)

    def backup_to(self, directory: Path | str) -> Path:
        """Create a consistent SQLite backup without copying a live WAL file."""
        target_directory = Path(directory)
        target_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = target_directory / f"series-{stamp}.db"
        with self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target
