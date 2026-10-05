"""Isolated development checks; uses the existing no-egress backend harness."""
import os
import asyncio  # Load Windows subprocess classes before no-egress monkeypatch.
import runpy
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / '.env').exists():
    raise RuntimeError('Use a clean checkout without .env for these tests.')

if os.name == 'nt':
    # Same single-process Windows migration shim as scripts/test_services.py.
    sys.modules['fcntl'] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *a: None)
    if not hasattr(os, 'getuid'):
        os.getuid = lambda: 0
    original_temp = tempfile.TemporaryDirectory
    def temporary_directory(*args, **kwargs):
        kwargs.setdefault('ignore_cleanup_errors', True)
        return original_temp(*args, **kwargs)
    tempfile.TemporaryDirectory = temporary_directory

if '--manifest' in sys.argv:
    # Mechanical schema snapshot on a disposable synthetic database only.
    import hashlib
    import json
    import sqlite3
    import app.schema_migrations as migrations
    with tempfile.TemporaryDirectory(prefix='multiwarehouse-manifest-') as directory:
        path = Path(directory) / 'catalog.db'
        registry = migrations.MIGRATIONS
        migrations.MIGRATIONS = registry[:-1]
        migrations.verify_complete_catalog_contract = lambda *a, **kw: True
        migrations.apply_migrations(path, app_commit='local-manifest')
        with sqlite3.connect(str(path)) as c:
            before = migrations._json_structure(c)
        migrations.MIGRATIONS = registry
        migrations.apply_migrations(path, app_commit='local-manifest')
        with sqlite3.connect(str(path)) as c:
            after = migrations._json_structure(c)
    delta = {'tables': {k: v for k, v in after['tables'].items() if before['tables'].get(k) != v}}
    for kind in ('indexes', 'triggers', 'views'):
        delta[kind] = [r for r in after[kind] if r not in before[kind]]
    (ROOT / 'app/catalog_multiwarehouse_schema_manifest.json').write_text(json.dumps(delta, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    inventory_path = ROOT / 'docs/runtime-ddl-inventory.json'
    inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
    modules = inventory['migration_modules']
    for file in ('app/schema_migrations.py', 'app/multiwarehouse_migration.py', 'app/catalog_multiwarehouse_schema_manifest.json'):
        existing = next((m for m in modules if m['file'] == file), None)
        if existing is None:
            existing = {'file': file, 'owner': 'migration-platform', 'reason': 'Versioned multiwarehouse migration; no runtime DDL'}
            modules.append(existing)
        existing['sha256'] = hashlib.sha256((ROOT / file).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Generated multiwarehouse schema contract from disposable databases.')
else:
    sys.argv = ['run_backend_tests.py', '--pattern', sys.argv[1] if len(sys.argv) > 1 else 'test_multiwarehouse.py']
    runpy.run_path(str(ROOT / 'scripts/run_backend_tests.py'), run_name='__main__')
