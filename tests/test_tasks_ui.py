"""Stage E presentation adapters: real HTTP, SQLite and centralized permissions."""
import sqlite3
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, request

from app.tasks.migrations import migrate_database
from app.tasks.repository import TasksRepository
from app.tasks_boundary import register_tasks_module


class TasksUiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'tasks-module.db'
        migrate_database(self.path)
        self.users = {i: {'id': i, 'active': 1, 'role': 'admin' if i == 4 else 'employee'} for i in range(1, 5)}
        self.actor = self.users[1]
        self.directory = mock.Mock(return_value=[{'id': i, 'name': 'User {}'.format(i)} for i in range(1, 5)])
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='synthetic', TASKS_MODULE_ENABLED=True, TASKS_MODULE_DATABASE=str(self.path))
        self.assertTrue(register_tasks_module(self.app, self.root, lambda: self.actor, self.users.get,
                                             lambda: request.headers.get('X-CSRF-Token') == 'test', self.directory))
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': 'test'}
        self.base = '/api/v1/tasks-module'

    def post(self, path, values):
        response = self.client.post(self.base + path, json=values, headers=self.headers)
        self.assertIn(response.status_code, (200, 201), response.get_json())
        return response.get_json()['data']

    def test_task_capabilities_creator_assignee_viewer_and_admin(self):
        project = self.post('/projects', {'name': 'Project'})
        self.post('/projects/{}/members'.format(project['id']), {'version': 1, 'user_id': 3})
        task = self.post('/tasks', {'title': 'Policy', 'assigned_to': 2, 'project_id': project['id']})
        expected = {1: (True, True, True), 2: (True, False, False), 3: (False, False, False), 4: (True, True, True)}
        for actor_id, rights in expected.items():
            self.actor = self.users[actor_id]
            direct = self.client.get(self.base + '/tasks/' + str(task['id'])).get_json()['data']
            listed = self.client.get(self.base + '/tasks?scope=all').get_json()['data']['items'][0]
            self.assertEqual(direct['permissions'], listed['permissions'])
            self.assertEqual(tuple(direct['permissions'][key] for key in ('edit', 'reassign', 'delete')), rights)
        self.actor = self.users[3]
        denied = self.client.patch(self.base + '/tasks/' + str(task['id']), json={'version': 1, 'title': 'No'}, headers=self.headers)
        self.assertEqual(denied.status_code, 403)

    def test_capabilities_not_mass_assignable_or_persisted_in_history(self):
        response = self.client.post(self.base + '/tasks', json={'title': 'Spoof', 'permissions': {'edit': True}}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        task = self.post('/tasks', {'title': 'Real'})
        history = self.client.get(self.base + '/tasks/{}/activity'.format(task['id'])).get_json()['data']['items']
        self.assertNotIn('permissions', history[0]['payload']['task'])
        deleted = self.post('/tasks/{}/delete'.format(task['id']), {'version': 1})
        self.assertTrue(deleted['permissions']['restore'])
        self.assertFalse(deleted['permissions']['edit'])

    def test_project_capabilities_are_owner_admin_only(self):
        project = self.post('/projects', {'name': 'Project'})
        self.post('/projects/{}/members'.format(project['id']), {'version': 1, 'user_id': 2})
        for actor_id, manage in ((1, True), (2, False), (4, True)):
            self.actor = self.users[actor_id]
            data = self.client.get(self.base + '/projects/{}'.format(project['id'])).get_json()['data']
            self.assertEqual(data['permissions'], {'manage': manage})
        self.actor = self.users[3]
        self.assertEqual(self.client.get(self.base + '/projects/{}'.format(project['id'])).status_code, 404)

    def test_directory_authorization_no_tasks_storage_and_safe_failure(self):
        with mock.patch.object(TasksRepository, 'transaction', side_effect=AssertionError('directory opened Tasks')) as storage:
            response = self.client.get(self.base + '/directory')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['data']['items'], self.directory.return_value)
            self.assertRegex(response.get_json()['data']['business_date'], r'^\d{4}-\d{2}-\d{2}$')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        storage.assert_not_called()
        self.actor = None
        self.directory.reset_mock()
        self.assertEqual(self.client.get(self.base + '/directory').status_code, 401)
        self.directory.assert_not_called()
        self.actor = self.users[1]
        self.directory.side_effect = RuntimeError('private path and SQL')
        response = self.client.get(self.base + '/directory')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private path', response.get_data(as_text=True))

    def test_disabled_registered_ui_and_directory_do_not_touch_storage(self):
        self.app.config['TASKS_MODULE_ENABLED'] = False
        with mock.patch.object(TasksRepository, 'transaction', side_effect=AssertionError('OFF storage')) as storage:
            for path in ('/app/tasks-module', self.base + '/directory', self.base + '/tasks'):
                self.assertEqual(self.client.get(path).status_code, 503)
        storage.assert_not_called()
        self.directory.assert_not_called()

    def test_ui_registration_failure_disables_partially_registered_api(self):
        app = Flask('registration-failure')
        app.config.update(TESTING=True, TASKS_MODULE_ENABLED=True, TASKS_MODULE_DATABASE=str(self.path))
        import importlib
        original = importlib.import_module
        def failure(name):
            if name == 'app.tasks.ui_routes':
                raise ImportError('UI failure')
            return original(name)
        with mock.patch('app.tasks_boundary.importlib.import_module', side_effect=failure):
            self.assertFalse(register_tasks_module(app, self.root, lambda: self.actor, self.users.get, lambda: True))
        self.assertFalse(app.extensions['tasks_module']['registered'])
        with mock.patch.object(TasksRepository, 'transaction', side_effect=AssertionError('partial registration storage')) as storage:
            self.assertEqual(app.test_client().get(self.base + '/tasks').status_code, 503)
        storage.assert_not_called()

    def test_read_only_directory_returns_only_active_minimal_identities(self):
        from app.auth import AuthStore
        from app.domain_schema_migrations import apply_domain_migrations
        path = self.root / 'auth.db'
        apply_domain_migrations(path, 'auth', 'tasks-ui-test')
        connection = sqlite3.connect(str(path))
        for user_id in (1, 2):
            connection.execute("INSERT INTO users(id,first_name,last_name,email,email_normalized,password_hash,role,active,created_at) VALUES(?,'Имя','日本語',?,?,'secret','employee',?,1)",
                               (user_id, '{}@example.test'.format(user_id), '{}@example.test'.format(user_id), user_id == 1))
        connection.commit()
        connection.close()
        before = path.read_bytes()
        original, opened = sqlite3.connect, []
        def read_only(database, *args, **kwargs):
            opened.append(str(database))
            self.assertTrue(str(database).endswith('auth.db?mode=ro'))
            conn = original(database, *args, **kwargs)
            conn.set_authorizer(lambda action, a, b, c, d: sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_ATTACH) else sqlite3.SQLITE_OK)
            return conn
        store = AuthStore(path)
        with mock.patch('sqlite3.connect', side_effect=read_only):
            self.assertEqual(store.list_active_task_identities(), [{'id': 1, 'name': 'Имя 日本語'}])
        self.assertEqual(len(opened), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_ui_shell_requires_auth_and_does_not_resolve_tasks(self):
        with mock.patch('app.tasks.ui_routes.render_template', return_value='synthetic shell') as template, \
                mock.patch.object(TasksRepository, 'transaction', side_effect=AssertionError('shell storage')) as storage:
            response = self.client.get('/app/tasks-module')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            template.assert_called_once()
            self.actor = None
            self.assertEqual(self.client.get('/app/tasks-module').status_code, 401)
        storage.assert_not_called()

    def test_real_erp_and_ui_shell_render_without_any_tasks_connections(self):
        import test_tasks_isolation as isolation
        fixture = isolation.TasksIsolationTest()
        fixture.setUp()
        try:
            with mock.patch.dict(os.environ, {'ERP_TASKS_MODULE_ENABLED': '1',
                    'ERP_TASKS_MODULE_DATABASE': str(fixture.module), 'ERP_AUTH_DATABASE': str(fixture.auth),
                    'ERP_TASKS_DATABASE': str(fixture.legacy)}):
                namespace = runpy.run_path(isolation.web.__file__, run_name='tasks_ui_isolation')
            app = namespace['app']
            app.config.update(TESTING=True, AUTH_TESTING=True, AUTH_ENABLED=True, SESSION_COOKIE_SECURE=False)
            client = app.test_client()
            fixture.login(client)
            calls, guard = fixture.reject_task_connections()
            with guard, mock.patch.dict(namespace['orders_page'].__globals__, {'get_orders': lambda *args, **kwargs: []}):
                fixture.render_core(client)
                response = client.get('/app/tasks-module')
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('tm-micro-form', html)
                self.assertIn('tasks-module-dialogs.js', html)
                self.assertIn('data-tasks-module-badge', html)
                self.assertIn('href="/app/tasks"', html)
                self.assertIn('href="/app/tasks-module"', html)
                app.config['TASKS_MODULE_ENABLED'] = False
                core = client.get('/app/orders').get_data(as_text=True)
                self.assertNotIn('tasks-module-notifications.js', core)
                self.assertNotIn('data-tasks-module-badge', core)
                self.assertEqual(client.get('/app/tasks-module').status_code, 503)
            self.assertEqual(calls, [])
            self.assertFalse(fixture.module.exists())
        finally:
            fixture.tearDown()
            fixture.doCleanups()
