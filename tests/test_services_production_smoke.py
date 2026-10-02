import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.services_production_smoke import cleanup_audit


class ServicesProductionSmokeCleanupTest(unittest.TestCase):
    def test_cleanup_removes_only_codex_service_smoke_audit_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with closing(sqlite3.connect(str(database))) as connection:
                connection.execute(
                    "CREATE TABLE erp_audit_events ("
                    "id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, "
                    "object_label_snapshot TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO erp_audit_events "
                    "(entity_type,object_label_snapshot) VALUES (?,?)",
                    (
                        ("service", "Codex Services smoke 4deb4c526629"),
                        ("service", "Codex Services smoke 451ab1aee13b"),
                        ("service", "Codex Services smoke not-a-token"),
                        ("service", "Рабочий сервис"),
                        ("order", "Codex Services smoke 4deb4c526629"),
                    ),
                )
                connection.commit()

            cleanup_audit(str(database))

            with closing(sqlite3.connect(str(database))) as connection:
                rows = connection.execute(
                    "SELECT entity_type,object_label_snapshot "
                    "FROM erp_audit_events ORDER BY id"
                ).fetchall()

            self.assertEqual(rows, [
                ("service", "Codex Services smoke not-a-token"),
                ("service", "Рабочий сервис"),
                ("order", "Codex Services smoke 4deb4c526629"),
            ])


if __name__ == "__main__":
    unittest.main()
