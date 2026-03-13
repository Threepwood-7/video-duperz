"""Public database entry point composed from focused mixin modules."""

from __future__ import annotations

from .db_action_history import DatabaseActionHistoryMixin
from .db_artifacts import DatabaseArtifactMixin
from .db_connection import DatabaseConnectionMixin
from .db_duplicate_groups import DatabaseDuplicateGroupMixin
from .db_scan_queries import DatabaseScanQueryMixin


class Database(
    DatabaseConnectionMixin,
    DatabaseArtifactMixin,
    DatabaseScanQueryMixin,
    DatabaseDuplicateGroupMixin,
    DatabaseActionHistoryMixin,
):
    """Repository-style wrapper around the application's SQLite database."""
