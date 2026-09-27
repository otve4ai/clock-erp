"""Final security acceptance against real routes/services and disposable SQLite."""
import sqlite3
import unittest
from unittest import mock

import test_tasks_ui as fixtures
from app.tasks.repository import TasksRepository


class TasksAcceptanceTest(unittest.TestCase):
    setUp = fixtures.TasksUiTest.setUp
    post = fixtures.TasksUiTest.post

    def test_every_mutation_rejects_missing_csrf_before_storage(self):
        calls = [('POST', '/tasks'), ('PATCH', '/tasks/1'), ('POST', '/tasks/1/status'),
                 ('POST', '/tasks/1/delete'), ('POST', '/tasks/1/restore'),
                 ('POST', '/microtasks'), ('PATCH', '/microtasks/1'),
                 ('POST', '/microtasks/1/convert'), ('POST', '/projects'),
                 ('PATCH', '/projects/1'), ('POST', '/projects/1/archive'),
                 ('POST', '/projects/1/restore'), ('POST', '/projects/1/members'),
                 ('DELETE', '/projects/1/members/2'), ('POST', '/inbox/1/read'),
                 ('POST', '/notifications/claim')]
        with mock.patch.object(TasksRepository, 'transaction', side_effect=AssertionError('CSRF opened storage')) as storage:
            for method, route in calls:
                response = self.client.open(self.base + route, method=method, json={'version': 1})
                self.assertEqual(response.status_code, 403, (method, route, response.get_json()))
        storage.assert_not_called()

    def test_sql_and_html_payload_remain_literal_and_private(self):
        title = "' OR 1=1 -- <img src=x onerror=alert(1)> 日本語 العربية 🙂"
        project = self.post('/projects', {'name': title})
        task = self.post('/tasks', {'title': title, 'description': '<script>alert(1)</script>', 'project_id': project['id']})
        own = self.client.get(self.base + '/tasks', query_string={'scope': 'all', 'search': "' OR 1=1 --"}).get_json()['data']
        self.assertEqual(own['total'], 1)
        self.assertEqual(own['items'][0]['title'], title)
        self.actor = self.users[2]
        for path in ('/tasks/' + str(task['id']), '/tasks/{}/activity'.format(task['id']), '/projects/' + str(project['id'])):
            self.assertEqual(self.client.get(self.base + path).status_code, 404)
        for path in ('/tasks', '/projects'):
            data = self.client.get(self.base + path, query_string={'search': "' OR 1=1 --"}).get_json()['data']
            self.assertEqual(data['total'], 0)
        self.assertEqual(self.client.get(self.base + '/tasks/summary').get_json()['data']['in_progress'], 0)

    def test_corrupt_storage_error_does_not_leak_details_or_repair(self):
        self.path.write_bytes(b'not a database: synthetic-corruption')
        before = self.path.read_bytes()
        response = self.client.get(self.base + '/tasks')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('database', response.get_data(as_text=True).lower())
        self.assertNotIn(str(self.path), response.get_data(as_text=True))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.get(self.base + '/directory').status_code, 200)

    def test_tasks_business_connections_are_exclusively_own_database(self):
        original, opened = sqlite3.connect, []
        def own_only(path, *args, **kwargs):
            opened.append(str(path))
            self.assertIn(str(path), (self.path.as_uri() + '?mode=rw', self.path.as_uri() + '?mode=ro'))
            return original(path, *args, **kwargs)
        with mock.patch('sqlite3.connect', side_effect=own_only):
            project = self.post('/projects', {'name': 'Isolated'})
            self.post('/projects/{}/members'.format(project['id']), {'version': 1, 'user_id': 2})
            task = self.post('/tasks', {'title': 'Isolated', 'assigned_to': 2, 'project_id': project['id']})
            micro = self.post('/microtasks', {'title': 'Isolated micro', 'assigned_to': 2})
            self.post('/tasks/{}/status'.format(task['id']), {'version': 1, 'status': 'done'})
            self.post('/microtasks/{}/convert'.format(micro['id']), {'version': 1})
            self.actor = self.users[2]
            event = self.client.get(self.base + '/inbox').get_json()['data']['items'][0]
            self.post('/tasks/{}/accept'.format(event['task_id']), {'version': event['version']})
            self.post('/notifications/claim', {})
        self.assertGreater(len(opened), 8)
