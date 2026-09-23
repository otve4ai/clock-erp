#!/usr/bin/env python3
"""Run the unittest suite with external network name resolution disabled."""

from __future__ import print_function

import argparse
import ipaddress
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for secret_name in (
    "MOYSKLAD_TOKEN",
    "BITRIX_LOGIN",
    "BITRIX_PASSWORD",
    "BITRIX_CATALOG_TOKEN",
    "BITRIX_ORDERS_TOKEN",
    "BITRIX_ORDER_COMMENTS_TOKEN",
    "UPDATE_ORDER_STATUS_TOKEN",
    "WB_API_TOKEN",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
):
    os.environ[secret_name] = ""
os.environ["ERP_TEST_MODE"] = "1"
for proxy_name in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(proxy_name, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.domain_schema_migrations import apply_domain_migrations  # noqa: E402
from app.catalog_db import CatalogDatabase  # noqa: E402
from app.schema_migrations import apply_migrations  # noqa: E402
from app.purchases_migrations import migrate_database as migrate_purchases  # noqa: E402
from app.customer_registry_migrations import migrate_database as migrate_customers  # noqa: E402
from app.sms_migrations import migrate_database as migrate_sms  # noqa: E402
from app.mail_migrations import migrate_database as migrate_mail  # noqa: E402


ORIGINAL_GETADDRINFO = socket.getaddrinfo
ORIGINAL_SOCKET_CONNECT = socket.socket.connect
ORIGINAL_CREATE_CONNECTION = socket.create_connection
ORIGINAL_POPEN = subprocess.Popen
ORIGINAL_CATALOG_INITIALIZE = CatalogDatabase.initialize


def install_legacy_fixture_bridge(path):
    """Keep pre-multiwarehouse test assertions useful without production writes.

    Older fixtures seed and inspect the retired columns directly.  The bridge
    exists only inside this isolated runner; targeted multiwarehouse tests run
    without it and assert that production services never write legacy stock.
    """
    import sqlite3
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS test_bridge_warehouse_to_product_insert
        AFTER INSERT ON erp_product_warehouse_stock
        WHEN NEW.warehouse_id=(SELECT id FROM erp_warehouses WHERE code='udelnaya')
          AND NOT EXISTS (SELECT 1 FROM erp_component_inventory WHERE product_id=NEW.product_id)
          AND NOT EXISTS (SELECT 1 FROM erp_product_bundles WHERE product_id=NEW.product_id)
        BEGIN
          UPDATE catalog_excel_products SET /* test fixture bridge */ stock=NEW.quantity WHERE id=NEW.product_id;
          UPDATE erp_component_inventory SET /* test fixture bridge */ physical_stock=NEW.quantity
            WHERE product_id=NEW.product_id;
        END;
        CREATE TRIGGER IF NOT EXISTS test_bridge_warehouse_to_product_update
        AFTER UPDATE OF quantity ON erp_product_warehouse_stock
        WHEN NEW.warehouse_id=(SELECT id FROM erp_warehouses WHERE code='udelnaya')
          AND NOT EXISTS (SELECT 1 FROM erp_component_inventory WHERE product_id=NEW.product_id)
          AND NOT EXISTS (SELECT 1 FROM erp_product_bundles WHERE product_id=NEW.product_id)
        BEGIN
          UPDATE catalog_excel_products SET /* test fixture bridge */ stock=NEW.quantity WHERE id=NEW.product_id;
          UPDATE erp_component_inventory SET /* test fixture bridge */ physical_stock=NEW.quantity
            WHERE product_id=NEW.product_id;
        END;
        CREATE TRIGGER IF NOT EXISTS test_bridge_product_to_warehouse
        AFTER UPDATE OF stock ON catalog_excel_products
        WHEN NEW.stock >= 0
        BEGIN
          INSERT OR IGNORE INTO erp_product_warehouse_stock
            (product_id,warehouse_id,quantity,initialized_at,updated_at)
          VALUES (NEW.id,(SELECT id FROM erp_warehouses WHERE code='udelnaya'),
            NEW.stock,datetime('now'),datetime('now'));
          UPDATE erp_product_warehouse_stock SET quantity=NEW.stock,
            initialized_at=COALESCE(initialized_at,datetime('now')),
            updated_at=datetime('now')
          WHERE product_id=NEW.id AND warehouse_id=(
            SELECT id FROM erp_warehouses WHERE code='udelnaya');
        END;
        CREATE TRIGGER IF NOT EXISTS test_bridge_product_insert_to_warehouse
        AFTER INSERT ON catalog_excel_products
        WHEN NEW.stock >= 0
        BEGIN
          INSERT OR IGNORE INTO erp_product_warehouse_stock
            (product_id,warehouse_id,quantity,initialized_at,updated_at)
          VALUES (NEW.id,(SELECT id FROM erp_warehouses WHERE code='udelnaya'),
            NEW.stock,datetime('now'),datetime('now'));
        END;
        CREATE TRIGGER IF NOT EXISTS test_bridge_component_to_warehouse
        AFTER UPDATE OF physical_stock ON erp_component_inventory
        WHEN NEW.physical_stock IS NOT NULL AND NEW.physical_stock >= 0
        BEGIN
          INSERT OR IGNORE INTO erp_product_warehouse_stock
            (product_id,warehouse_id,quantity,initialized_at,updated_at)
          VALUES (NEW.product_id,(SELECT id FROM erp_warehouses WHERE code='udelnaya'),
            NEW.physical_stock,datetime('now'),datetime('now'));
          UPDATE erp_product_warehouse_stock SET quantity=NEW.physical_stock,
            initialized_at=COALESCE(initialized_at,datetime('now')),
            updated_at=datetime('now')
          WHERE product_id=NEW.product_id AND warehouse_id=(
            SELECT id FROM erp_warehouses WHERE code='udelnaya');
        END;
        """)
        connection.commit()
    finally:
        connection.close()


def initialize_test_catalog(database, allow_schema_changes=False):
    """Migrate only brand-new fixture files before runtime validation.

    Existing files stay untouched so migration/guard tests still exercise the
    real production behavior.  The production initializer still performs its
    complete validation and cache behavior after the fixture is prepared.
    """
    if str(database.path) != ":memory:":
        path = Path(database.path).resolve()
        if not path.exists():
            apply_migrations(path, app_commit="test-suite")
            install_legacy_fixture_bridge(path)
    return ORIGINAL_CATALOG_INITIALIZE(
        database,
        allow_schema_changes=allow_schema_changes,
    )


def local_host(host):
    value = str(host or "").strip().strip("[]")
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def guarded_getaddrinfo(host, *args, **kwargs):
    if not local_host(host):
        raise OSError("external network disabled during backend tests")
    return ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def _address_host(address):
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def guarded_socket_connect(sock, address):
    host = _address_host(address)
    if host is not None and not local_host(host):
        raise OSError("external network disabled during backend tests")
    return ORIGINAL_SOCKET_CONNECT(sock, address)


def guarded_create_connection(address, *args, **kwargs):
    host = _address_host(address)
    if host is not None and not local_host(host):
        raise OSError("external network disabled during backend tests")
    return ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def guarded_popen(args, *pargs, **kwargs):
    command = args if isinstance(args, (list, tuple)) else [args]
    executable = Path(str(command[0] or "")).name.casefold() if command else ""
    if executable in {"curl", "wget"}:
        raise OSError("external network subprocess disabled during backend tests")
    return ORIGINAL_POPEN(args, *pargs, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--verbosity", type=int, default=2)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vechasu-backend-tests-") as root:
        test_root = Path(root).resolve()
        catalog_path = test_root / "catalog.db"
        auth_path = test_root / "auth.db"
        orders_path = test_root / "orders.db"
        tasks_path = test_root / "tasks.db"
        purchases_path = test_root / "purchases.db"
        customers_path = test_root / "customers.db"
        sms_path = test_root / "sms.db"
        mail_path = test_root / "mail.db"
        isolated_environment = {
            "CATALOG_DATABASE_PATH": str(catalog_path),
            "ERP_AUTH_DATABASE": str(auth_path),
            "ORDERS_DATABASE_PATH": str(orders_path),
            "ERP_TASKS_DATABASE": str(tasks_path),
            "ERP_PURCHASES_DATABASE": str(purchases_path),
            "CUSTOMERS_DATABASE_PATH": str(customers_path),
            "ERP_SMS_DATABASE": str(sms_path),
            "ERP_MAIL_DATABASE": str(mail_path),
            "ERP_MAIL_ATTACHMENT_ROOT": str(test_root / "mail-attachments"),
            "ERP_MAIL_SECRET_KEY": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
            "ERP_SECRET_KEY": "isolated-backend-test-secret",
            "ERP_TEST_ROOT": str(test_root),
        }
        previous_environment = {
            name: os.environ.get(name) for name in isolated_environment
        }
        os.environ.update(isolated_environment)

        socket.getaddrinfo = guarded_getaddrinfo
        socket.socket.connect = guarded_socket_connect
        socket.create_connection = guarded_create_connection
        subprocess.Popen = guarded_popen
        CatalogDatabase.initialize = initialize_test_catalog
        if __name__ == "__main__":
            sys.modules["scripts.run_backend_tests"] = sys.modules[__name__]
        try:
            apply_migrations(catalog_path, app_commit="test-suite")
            install_legacy_fixture_bridge(catalog_path)
            apply_domain_migrations(auth_path, "auth", "test-suite")
            apply_domain_migrations(orders_path, "orders", "test-suite")
            apply_domain_migrations(tasks_path, "tasks", "test-suite")
            migrate_purchases(purchases_path)
            migrate_customers(customers_path)
            migrate_sms(sms_path)
            migrate_mail(mail_path)
            suite = unittest.defaultTestLoader.discover(
                arguments.start_directory,
                pattern=arguments.pattern,
            )
            result = unittest.TextTestRunner(
                verbosity=arguments.verbosity,
            ).run(suite)
            return 0 if result.wasSuccessful() else 1
        finally:
            socket.getaddrinfo = ORIGINAL_GETADDRINFO
            socket.socket.connect = ORIGINAL_SOCKET_CONNECT
            socket.create_connection = ORIGINAL_CREATE_CONNECTION
            subprocess.Popen = ORIGINAL_POPEN
            CatalogDatabase.initialize = ORIGINAL_CATALOG_INITIALIZE
            for name, value in previous_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
