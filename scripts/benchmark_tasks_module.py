#!/usr/bin/env python3
"""Synthetic Tasks acceptance benchmark. Creates and deletes only its own temp DB.

No ERP imports, auth, live databases or network. Timings include real service,
permissions, schema validation and transaction overhead. Not a production SLA.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import platform
from pathlib import Path
import socket
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    from app.tasks.migrations import migrate_database
    from app.tasks.repository import TasksRepository
    from app.tasks.services import TasksService
    from app.tasks.project_services import ProjectsService
    from app.tasks.inbox_services import InboxService

    original_connect, original_network = sqlite3.connect, socket.socket.connect
    stamp = '2026-09-27T10:00:00+00:00'
    user = {'id': 2, 'active': 1, 'role': 'employee'}
    admin = {'id': 1, 'active': 1, 'role': 'admin'}
    queries = {}
    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, parameters=()):
            if sql.startswith('SELECT'):
                queries[sql] = list(parameters)
            return self.connection.execute(sql, parameters)
    class RecordingRepository(TasksRepository):
        @contextmanager
        def transaction(self, write=False):
            with super(RecordingRepository, self).transaction(write) as session:
                session.connection = RecordingConnection(session.connection)
                yield session
    def no_network(*args, **kwargs):
        raise AssertionError('Benchmark must never use network')

    with tempfile.TemporaryDirectory(prefix='tasks-benchmark-') as temp:
        database = Path(temp) / 'tasks-module.db'
        def guarded_connect(path, *positional, **kwargs):
            if str(path) not in (str(database), database.as_uri() + '?mode=ro', database.as_uri() + '?mode=rw'):
                raise AssertionError('Benchmark attempted another database')
            return original_connect(path, *positional, **kwargs)
        sqlite3.connect, socket.socket.connect = guarded_connect, no_network
        try:
            migrate_database(database)
            connection = sqlite3.connect(str(database))
            connection.execute('PRAGMA foreign_keys=ON')
            connection.executemany('INSERT INTO task_projects(id,name,owner_id,created_at,updated_at) VALUES(?,?,?,?,?)',
                                   [(i, 'Synthetic project {}'.format(i), 1 + i % 100, stamp, stamp) for i in range(1, 501)])
            connection.executemany('INSERT INTO task_project_members(project_id,user_id,created_at) VALUES(?,?,?)',
                                   [(i, 2, stamp) for i in range(1, 501, 20)])
            rows = []
            for i in range(1, 30001):
                # Vary status independently of user_id; every user has active/done
                # tasks, rather than accidentally benchmarking an empty my view.
                status = ('new', 'in_progress', 'waiting', 'done')[(i // 100) % 4]
                rows.append((i, 'Task {} {}'.format(i, 'needle' if i % 51 == 0 else ''), 'Synthetic description', status,
                             1 + i % 100, 1 + (i * 7) % 100, '2026-09-{:02d}'.format(1 + (i // 100) % 30) if (i // 100) % 5 else None,
                             stamp, stamp, stamp if status == 'done' else None, 1 + i % 500 if i % 7 else None))
            connection.executemany('INSERT INTO tasks(id,title,description,status,created_by,assigned_to,deadline_date,created_at,updated_at,completed_at,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)', rows)
            connection.executemany("INSERT INTO tasks(id,task_type,title,created_by,assigned_to,created_at,updated_at,micro_deadline_at) VALUES(?,'micro',?,?,?,?,?,?)",
                                   [(30000 + i, 'Micro {}'.format(i), 1, 1 + i % 100, stamp, stamp, '2026-09-28T10:00:00+00:00') for i in range(1, 2001)])
            connection.executemany("INSERT INTO task_activity(task_id,actor,event_type,timestamp,task_version,payload) VALUES(1,2,'content_changed',?,?,'{}')",
                                   [(stamp, i) for i in range(1, 10001)])
            connection.execute('UPDATE tasks SET version=10000 WHERE id=1')
            connection.executemany("INSERT INTO task_inbox_events(recipient_id,task_id,actor_id,event_type,created_at,dedupe_key,payload) VALUES(?,?,1,'task_assigned',?,?,?)",
                                   [(1 + i % 100, 30000 + i, stamp, 'seed-' + str(i), json.dumps({'title': 'Micro {}'.format(i), 'task_type': 'micro'})) for i in range(1, 2001)])
            connection.commit()
            connection.close()
            repository = RecordingRepository(database)
            tasks = TasksService(repository, lambda uid: dict(user, id=uid), now=lambda: stamp, today=lambda: '2026-09-27')
            projects = ProjectsService(repository, today=lambda: '2026-09-27')
            inbox = InboxService(repository)
            cases = {}
            for scope in ('my', 'created', 'team', 'all'):
                for offset in (0, 150):
                    cases[scope + '_offset_' + str(offset)] = lambda scope=scope, offset=offset: tasks.list(user, {'scope': scope, 'limit': 30, 'offset': offset})
            cases.update({
                'admin_all': lambda: tasks.list(admin, {'scope': 'all'}),
                'admin_deep_15000': lambda: tasks.list(admin, {'scope': 'all', 'offset': 15000}),
                'today': lambda: tasks.list(user, {'scope': 'all', 'view': 'today'}),
                'overdue': lambda: tasks.list(user, {'scope': 'all', 'view': 'overdue'}),
                'overdue_boolean': lambda: tasks.list(user, {'scope': 'all', 'overdue': 'true'}),
                'search': lambda: tasks.list(user, {'scope': 'all', 'search': 'needle'}),
                'archive': lambda: tasks.list(user, {'scope': 'all', 'view': 'archive'}),
                'projects_page_counters': lambda: projects.list(user, {'limit': 30}),
                'projects_admin_100': lambda: projects.list(admin, {'limit': 100}),
                'activity_deep': lambda: tasks.activity(admin, 1, {'limit': 100, 'offset': 9000}),
                'summary': lambda: tasks.list(user, summary=True),
                'micro_list': lambda: tasks.micros(user),
                'micro_summary': lambda: tasks.micros(user, summary=True),
                'inbox': lambda: inbox.list(user),
            })
            timings = {}
            for label, action in sorted(cases.items()):
                result = action()
                # Page timings must not silently measure an empty result.
                if 'offset_' in label or label == 'admin_deep_15000':
                    assert result['items'], label
                samples = []
                for _ in range(7):
                    start = time.perf_counter(); action(); samples.append((time.perf_counter() - start) * 1000)
                timings[label] = {'median_ms': round(statistics.median(samples), 3), 'max_ms': round(max(samples), 3), 'samples': 7}
            query_plans = []
            with repository.transaction() as session:
                raw = session.connection.connection
                for sql, params in sorted(queries.items()):
                    query_plans.append({'sql': sql, 'plan': [list(row) for row in raw.execute('EXPLAIN QUERY PLAN ' + sql, params)]})
            # Real concurrent connections: 20 local commits while a reader loops.
            mutable = tasks.create(admin, {'title': 'Concurrent benchmark', 'assigned_to': 1})
            read_stats = {'ok': 0, 'busy': 0, 'max_ms': 0, 'unexpected_errors': []}
            stop = threading.Event()
            def reader():
                while not stop.is_set():
                    start = time.perf_counter()
                    try:
                        tasks.list(user, {'scope': 'my'}); read_stats['ok'] += 1
                    except sqlite3.OperationalError as error:
                        if 'locked' in str(error): read_stats['busy'] += 1
                        else: read_stats['unexpected_errors'].append(type(error).__name__); stop.set()
                    except Exception as error:
                        read_stats['unexpected_errors'].append(type(error).__name__); stop.set()
                    read_stats['max_ms'] = max(read_stats['max_ms'], (time.perf_counter() - start) * 1000)
            thread = threading.Thread(target=reader); thread.start()
            writes = []
            try:
                for index in range(20):
                    start = time.perf_counter()
                    mutable = tasks.mutate(admin, mutable['id'], {'version': mutable['version'], 'title': 'Concurrent {}'.format(index)})
                    writes.append((time.perf_counter() - start) * 1000)
            finally:
                stop.set(); thread.join()
            read_stats['max_ms'] = round(read_stats['max_ms'], 3)
            assert read_stats['ok'] > 0 and not read_stats['unexpected_errors'], read_stats
            report = {'runtime': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'platform': platform.platform()},
                      'dataset': {'normal_tasks': 30000, 'microtasks': 2000, 'projects': 500, 'users': 100, 'activity_hot_task': 10000, 'inbox_events': 2000},
                      'timings': timings, 'query_plans': query_plans, 'concurrent_reader': read_stats,
                      'concurrent_writes': {'count': len(writes), 'median_ms': round(statistics.median(writes), 3), 'max_ms': round(max(writes), 3)},
                      'source_sha256': {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / 'app/tasks').glob('*.py'))}}
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            print(json.dumps({'runtime': report['runtime'], 'timings': timings, 'concurrent_reader': read_stats, 'concurrent_writes': report['concurrent_writes']}))
        finally:
            sqlite3.connect, socket.socket.connect = original_connect, original_network


if __name__ == '__main__':
    main()
