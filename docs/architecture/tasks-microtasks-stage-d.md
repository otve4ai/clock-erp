# Tasks Stage D — Inbox, notification claims, microtasks

Status: current, independent final review PASS. 2026-09-27.
Base: `8e7ae709e76e9b67d51b3f200e360f7c7aec034a`.
Branch: `codex/tasks-microtasks-stage-d`. Flag remains OFF by default.
No legacy reads/migration, UI, push, merge, deploy or production enable.
Roadmap: `tasks-roadmap-d-f.md`; the owner authorized automatic continuation through F.

## Contract

Schema v4 adds `tasks.micro_deadline_at TEXT NULL`, permits normal/micro, and adds
`converted_to_normal` Activity. SQL CHECK enforces normal -> NULL micro deadline;
micro -> non-NULL micro deadline, new/done status, NULL date deadline and project.
Micro creation accepts only title and optional assignee (default current user).
Generic PATCH cannot bypass the restricted title/assignee/new-or-done shape.
User and server timestamps cannot be supplied by clients.

One server UTC instant T produces created_at=T and micro_deadline_at=T+86400s.
Micro timestamps have six fractional digits and +00:00, for exact lexical ordering.
Python 3.6 strptime parses the server clock; no fromisoformat or host-local conversion.
Rename, reassign, complete/reopen and restore preserve the original deadline.
Overdue is strictly deadline < now and active/nondeleted; equality is not overdue.

Conversion uses the same row/ID, increments version, keeps creator/assignee/title,
created_at/status/completed_at and history, clears micro deadline, sets type normal.
Creator, assignee and admin can convert under existing edit permissions. Active
converted tasks enter the normal list; completed ones enter the normal Archive.
Until conversion microtasks have no project. Main lists/summary/views/Archive filter
normal; micro completed records remain available via explicit micro status=done.

## Inbox and delivery

`task_inbox_events` columns: id INTEGER PK, recipient_id INTEGER NOT NULL >0,
task_id INTEGER NOT NULL FK tasks(id), actor_id INTEGER NOT NULL >0,
event_type TEXT NOT NULL CHECK(task_assigned/task_reassigned), created_at TEXT NOT
NULL, handled_at TEXT NULL, notified_at TEXT NULL, dedupe_key TEXT NOT NULL UNIQUE,
payload TEXT NOT NULL (JSON snapshot title/type).

Assignment to another user creates an event. Self assignment is silent. Reassign
retires old recipient events (handled_at and unsent notified_at) and inserts the
new event unless new assignee is the actor. Dedupe key includes task/event/recipient/
task version; a later assignment back creates a distinct event. Retiring unsent
toasts additionally prevents a stale notification to a former assignee.

Only the recipient sees or handles their events, including for global admins.
Task GET/history do not write Inbox state. Explicit read is idempotent. Done/deleted
tasks do not automatically erase pending assignment events. Dashboard inbox count
now replaces the earlier null marker, independently of normal-task filters.
Inbox orders by created_at DESC, id DESC, including after server clock adjustments.
Micro summary's nearest deadline excludes already-overdue tasks; equality is included.

Toast claim uses its own short BEGIN IMMEDIATE transaction, takes <=10 pending
recipient events, marks notified_at and returns their snapshots. Parallel claims
do not return the same event. Claim never changes handled_at or Task/version.
Error before commit rolls the claim back; task creation has already committed.
No scheduler, email, SMS, shared audit or external delivery dependency.

Delivery guarantee is at-most-once retrieval, not guaranteed toast display: loss
of the HTTP response after claim commit can lose a toast. Durable Inbox is retained.
No create idempotency key: repeating an entire successful POST creation creates a
new task; UI must prevent duplicate submission. Versioned reassign retry returns409.

## API (prefix /api/v1/tasks-module)

| Method | Path | Behavior |
|---|---|---|
| GET / POST | /microtasks | list / create |
| GET | /microtasks/summary | my_active, created_active, team_active, my_overdue, nearest_my_deadline_at |
| PATCH | /microtasks/{id} | title/assignee/new-or-done, expected version |
| POST | /microtasks/{id}/complete | version, new -> done |
| POST | /microtasks/{id}/reopen | version, done -> new |
| POST | /microtasks/{id}/convert | version, micro -> normal |
| GET | /inbox | recipient's pending events; limit/offset |
| GET | /inbox/badge | read-only pending count |
| POST | /inbox/{id}/read | empty object; recipient only, idempotent |
| POST | /notifications/claim | empty object or integer limit 1..10 |

Micro list: scopes my/created/team/all with centralized Stage C visibility,
search(title), assignee, creator, overdue, new/done, pagination. Sort deadline/id.
Task GET/activity/delete/restore reuse existing endpoints and permissions.
Mutations retain CSRF, bounded cached/unread JSON handling and safe HTTP errors.

## Transactions, migration and indexes

Creation: Task + Activity + optional Inbox event in one local transaction.
Reassign: version CAS + Activity + retirement + new Inbox event, all atomic.
Conversion/completion/reopen: Task CAS + Activity atomically. Failure rolls back.
External active-user lookup finishes before a write transaction, with authorization
and version rechecked under the write lock. No auth writes in business layer.

Explicit offline migration validates frozen v3, copies its own Task/history to TEMP,
rebuilds changed tables and indexes, restores records, creates Inbox/ledger v4,
validates and commits. FK remains ON; DDL/FK failure restores the original DB.
Projects/members/project Activity are unchanged. Existing tasks receive no retroactive
Inbox events. Frozen v2 -> v3 migration still runs correctly before v3 -> v4.
No migrations during requests/startup. ATTACH/DETACH remain denied by authorizer.

Added indexes: tasks_micro_deadline(type,deleted_at,deadline,id), Inbox recipient/
handled/id, recipient/notified/id and task/recipient/handled. Unique dedupe constraint.
Micro summary uses three fixed aggregate queries; no per-row/project count loops.
Strict schema inspection checks new columns, CHECK/FK/default/PK and ledger/indexes.
TD-TASKS-001 remains for measured performance acceptance in Stage F.

## Validation

40 new tests cover exact24h/boundaries/timezones, full lifecycle, permissions/IDOR,
restricted fields, filters/scopes/counts, normal/micro separation, same-ID conversion,
concurrent updates and claims, read idempotence, generation dedupe, assignment and
claim rollbacks, v3 migration preservation/rollback/FK/idempotence, schema corruption,
HTTP limits/errors/CSRF, OFF and database isolation. Existing tests updated only for
schema v4, real Inbox count and valid corruption-fixture ledgers.

Final suite: 248 tests. Windows Python3.12.14/SQLite3.53.1:247 pass+1 Linux-only skip.
Exact Linux CentOS7.9/Python3.6.8/SQLite3.7.17/Flask2.0.3/Werkzeug2.0.3:
248 pass, no failures/errors/skips. DDL gate and SQL compatibility pass.
All 35 runtime source hashes match local canonical LF files.
Independent read-only review found MEDIUM nearest-summary semantics and LOW Inbox
ordering; both fixed with regression tests and full repeated exact-runtime validation.
Evidence: `docs/validation/tasks-microtasks-d-runtime.json`, `tasks-microtasks-d-ddl.json`.
Synthetic fixtures only, nobody UID99, empty env, OS network disabled, guarded DB paths.
New business operations open only tasks-module.db; auth lookup read-only. Legacy
tests exercise their own synthetic fixtures, not production or new-core legacy access.
Real Orders/Products/Sales/Stock renders still open neither Tasks DB; Linux independent
process IMMEDIATE/EXCLUSIVE locks are tested. Corrupt Tasks schema/claim remains local503.

## Files

- `app/tasks/domain.py`, `services.py`, `repository.py`, `routes.py`, `schema.py`, `migrations.py`.
- New `app/tasks/inbox_repository.py`, `inbox_services.py`, `micro_repository.py`.
- New `tests/test_tasks_microtasks.py`; updated core/schema/isolation/module/projects/runtime tests.
- `scripts/validate_tasks_runtime.py`, `docs/runtime-ddl-inventory.json`.
- This report, roadmap, document register and two runtime evidence JSON files.

## Remaining scope

No preference table: user-specific collapsed state belongs in Stage E localStorage.
No project microtasks, mentions, comments/checklists/files or new role system.
Review chat final verdict: **FINAL VERDICT STAGE D: PASS** after the two-issue delta.
Reviewer inspected source/diff/evidence; Codex executed tests. Temporary server copy
`/tmp/erp-tasks-d.kyAiOq8w` removed after evidence download and resolved-path check.
Production HEAD remains `a0e53422c023bfaa8bdc40759b2f4587ea650d1c`, service active;
no production code/data/settings/restart changes. Local commit SHA accompanies report.
Continue E without stopping for owner approval, retaining no push/merge/deploy restrictions.
