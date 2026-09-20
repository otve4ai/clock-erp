#!/usr/bin/env python3
"""Create or verify the isolated services vault schema (SQLite 3.7.17 safe)."""

import argparse
import sqlite3
from pathlib import Path

BASE_MIGRATION_ID = "2026-08-28-services-vault-v1"
MIGRATION_ID = "2026-09-20-service-categories-v2"
TABLES = (
    "services", "service_accounts", "service_permissions",
    "service_user_preferences", "service_categories",
)
DEFAULT_CATEGORIES = (
    ("sites", "Сайты", 10),
    ("sales", "Продажи", 20),
    ("delivery", "Доставка", 30),
    ("infrastructure", "Инфраструктура", 40),
)


def apply(database):
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS service_schema_migrations (migration_id TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")
        connection.execute("""CREATE TABLE IF NOT EXISTS service_categories (
            category_key TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            position INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, icon TEXT NOT NULL DEFAULT 'globe',
            icon_blob BLOB, icon_mime TEXT, created_by INTEGER NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, archived_at INTEGER, version INTEGER NOT NULL DEFAULT 1)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS service_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER NOT NULL, label TEXT NOT NULL,
            login_encrypted BLOB, password_encrypted BLOB, position INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS service_permissions (
            service_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            can_view INTEGER NOT NULL DEFAULT 0, can_open INTEGER NOT NULL DEFAULT 0,
            can_view_login INTEGER NOT NULL DEFAULT 0, can_copy_login INTEGER NOT NULL DEFAULT 0,
            can_view_password INTEGER NOT NULL DEFAULT 0, can_copy_password INTEGER NOT NULL DEFAULT 0,
            can_edit INTEGER NOT NULL DEFAULT 0, can_manage_access INTEGER NOT NULL DEFAULT 0,
            can_archive INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(service_id,user_id),
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS service_user_preferences (
            service_id INTEGER NOT NULL, user_id INTEGER NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL,
            PRIMARY KEY(service_id,user_id), FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE)""")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_services_archive ON services(archived_at,id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_service_accounts_service ON service_accounts(service_id,position,id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_service_permissions_user ON service_permissions(user_id,service_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_service_preferences_user ON service_user_preferences(user_id,favorite,sort_order)")
        ledger = {row[0] for row in connection.execute(
            "SELECT migration_id FROM service_schema_migrations"
        )}
        if ledger not in (set(), {BASE_MIGRATION_ID}, {BASE_MIGRATION_ID, MIGRATION_ID}):
            raise RuntimeError("services migration ledger differs")
        if MIGRATION_ID not in ledger:
            for category_key, name, position in DEFAULT_CATEGORIES:
                if connection.execute(
                    "SELECT 1 FROM service_categories WHERE category_key=?", (category_key,)
                ).fetchone() is None:
                    connection.execute(
                        "INSERT INTO service_categories(category_key,name,position,created_at,updated_at) "
                        "VALUES(?,?,?,strftime('%s','now'),strftime('%s','now'))",
                        (category_key, name, position),
                    )
        if not ledger:
            connection.execute(
                "INSERT INTO service_schema_migrations(migration_id,applied_at) VALUES(?,strftime('%s','now'))",
                (BASE_MIGRATION_ID,),
            )
            ledger.add(BASE_MIGRATION_ID)
        if ledger == {BASE_MIGRATION_ID}:
            connection.execute(
                "INSERT INTO service_schema_migrations(migration_id,applied_at) VALUES(?,strftime('%s','now'))",
                (MIGRATION_ID,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return verify(path)


def verify(database):
    connection = sqlite3.connect(str(database))
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        expected = set(TABLES) | {"service_schema_migrations"}
        if tables != expected:
            raise RuntimeError("services schema differs")
        ledger = {row[0] for row in connection.execute(
            "SELECT migration_id FROM service_schema_migrations"
        )}
        if ledger != {BASE_MIGRATION_ID, MIGRATION_ID}:
            raise RuntimeError("services migration ledger differs")
        if connection.execute("SELECT COUNT(*) FROM service_categories").fetchone()[0] < 1:
            raise RuntimeError("services categories are missing")
        if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
            raise RuntimeError("services quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("services foreign_key_check failed")
        return {table: connection.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0] for table in TABLES}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply", "verify"))
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    result = apply(args.database) if args.action == "apply" else verify(args.database)
    print("SERVICES_MIGRATION_OK {}".format(result))


if __name__ == "__main__":
    main()
