"""Scheduled, bounded followups; disabled until explicitly enabled in production."""
import argparse
import os
import sqlite3
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_environment(path):
    from dotenv import dotenv_values
    details = Path(path).stat()
    if details.st_uid != 0 or stat.S_IMODE(details.st_mode) != 0o600:
        raise ValueError('Protected configuration required')
    allowed = {'CDEK_ACCOUNT', 'CDEK_PASSWORD', 'CDEK_CACHE_DIR', 'CDEK_FOLLOWUPS_ENABLED',
               'SMSBLISS_LOGIN', 'SMSBLISS_PASSWORD', 'SMSBLISS_API_BASE_URL', 'ERP_SMS_DATABASE',
               'ERP_AUTH_DATABASE', 'ERP_TASKS_MODULE_DATABASE', 'ERP_TASKS_MODULE_ENABLED'}
    for key, value in dotenv_values(str(path), interpolate=False).items():
        if key in allowed and value is not None:
            os.environ[key] = value


def assignee(auth_path):
    with sqlite3.connect(Path(auth_path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute("SELECT id,active FROM users WHERE login=? AND lower(email)=? AND active=1",
                          ('ops', 'lera@mail.ru')).fetchall()
    if len(rows) != 1:
        raise ValueError('CDEK assignee identity not confirmed')
    return {'id': rows[0][0], 'active': rows[0][1]}


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--environment-file', action='append', default=[])
    parser.add_argument('--check', action='store_true', help='Validate configuration without sending or creating tasks')
    args = parser.parse_args()
    for path in args.environment_file:
        load_environment(path)
    if os.getenv('CDEK_FOLLOWUPS_ENABLED') != '1' and not args.check:
        print('CDEK_FOLLOWUPS=disabled')
        return 0
    with (ROOT / 'instance' / '.cdek-followups.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        from app.web import app, api_sales_records, CDEK_DELIVERY
        from app.auth import AuthStore
        from app.tasks.services import TasksService
        from app.tasks.repository import TasksRepository
        from app.services.sms import SmsService, SmsStore
        from app.clients.smsbliss import SmsBlissClient
        from app.services.cdek_sales import group_sales
        from app.services.cdek_followups import CdekFollowups
        if not CDEK_DELIVERY.client.configured:
            raise ValueError('CDEK is not configured')
        if str(app.config.get('TASKS_MODULE_ENABLED')).lower() not in {'1', 'true', 'yes', 'on'}:
            raise ValueError('Tasks module is disabled')
        person = assignee(app.config['AUTH_DATABASE'])
        auth = AuthStore(app.config['AUTH_DATABASE'])
        tasks = TasksService(TasksRepository(app.config['TASKS_MODULE_DATABASE']), auth.get_active_user_identity)
        tasks.status()
        client = SmsBlissClient()
        if not client.configured:
            raise ValueError('SMS is not configured')
        store = SmsStore(app.config['SMS_DATABASE'])
        store.verify()
        if args.check:
            print('CDEK_FOLLOWUPS_CONFIG=ok assignee=ops')
            return 0
        result = CdekFollowups(CDEK_DELIVERY, SmsService(store, client), tasks, person).run(group_sales(api_sales_records()))
        print('CDEK_FOLLOWUPS checked={checked} errors={errors}'.format(**result))
        return 1 if result['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('CDEK_FOLLOWUPS_FAILED: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
