# Tasks D–F roadmap

Status: planned; 2026-09-27. Review-chat coordination following the owner's authorization to continue through final acceptance. This plan is not implementation evidence. No push, merge, deploy or production enable.

Принято. От принятого Stage C `8e7ae709e76e9b67d51b3f200e360f7c7aec034a` дальше работаем непрерывно, без ожидания владельца после каждого этапа.

## Что считаем полным завершением разработки

Оставшихся этапа **три**, и я бы на этом зафиксировал scope, чтобы проект больше не разрастался.

**Stage D — Inbox + Notifications + Microtasks 24h.** Только backend-механика новых функций.

**Stage E — полный пользовательский UI.** Главный экран по утверждённому макету, Входящие, Сегодня, Ожидаю, Микрозадачи, Проекты, список/доска проекта, карточка задачи и **Архив по отдельно утверждённому макету**. Также task-toast/счётчики в общей оболочке ERP.

**Stage F — FINAL ACCEPTANCE.** Последний этап. Никаких новых функций: только E2E/UX, security, performance, exact-runtime, isolation, визуальная проверка всех экранов и итоговый отчёт.

После Stage F разработка считается **законченной, но не развёрнутой**. То есть должно быть:

- функционально готовое backend + UI;
- feature flag всё ещё `OFF`;
- отдельные локальные commits D/E/F;
- зелёные automated/E2E tests;
- проверка на Linux / Python 3.6.8 / SQLite 3.7.17;
- доказательство, что поломка Tasks не ломает ERP;
- финальные screenshots основных экранов, включая Archive;
- clean working tree;
- финальный commit hash.

**Push / merge / deploy / включение production — отдельное будущее решение владельца и в понятие завершения Stage F не входят.**

Не добавляем без нового запроса владельца: Calendar, Documentation, Gantt, time tracking, comments, checklist, attachments, HR-role system и прочие новые сущности. Сейчас они не нужны для согласованного результата.

---

# STAGE D — INBOX + NOTIFICATIONS + MICROTASKS 24H

Работать от:

`8e7ae709e76e9b67d51b3f200e360f7c7aec034a`

Новая ветка Stage D — от этого commit.

Feature flag остаётся OFF.

Legacy `tasks.db` не читать, не переносить и не изменять.

## D1. Общий принцип

Добавить только три пользовательские возможности:

**Inbox** — очередь новых назначений, которые пользователь ещё не разобрал.

**Notification** — одноразовый toast-сигнал о новом назначении, который позднее сможет получить общая оболочка ERP независимо от текущей страницы.

**Microtask** — маленькая задача с точным сроком `created_at + 24 часа`.

Не создавать для уведомлений отдельную сложную message-bus систему.

---

## D2. Schema v4

Предлагаю минимальную эволюцию `tasks-module.db`.

### `tasks`

Разрешить:

```text
task_type:
normal
micro
```

Добавить:

```text
micro_deadline_at TEXT NULL
```

Инварианты на уровне DB:

**normal:**
- `task_type='normal'`
- `micro_deadline_at IS NULL`

**micro:**
- `task_type='micro'`
- `micro_deadline_at IS NOT NULL`
- `status IN ('new','done')`
- `deadline_date IS NULL`
- `project_id IS NULL`

Обычные Tasks сохраняют Stage C statuses:

```text
new
in_progress
waiting
done
```

Microtasks используют только:

```text
new
done
```

Не вводить `active` как ещё один status.

### Inbox

Новая таблица примерно:

```text
task_inbox_events

id
recipient_id
task_id
actor_id
event_type
created_at
handled_at
notified_at
dedupe_key
payload
```

`dedupe_key` UNIQUE.

Разрешённые события Stage D:

```text
task_assigned
task_reassigned
```

`mention` сейчас НЕ делать — comments у нас вообще не входят в согласованный scope.

`payload` может хранить небольшой snapshot, необходимый toast:

```text
task title
task_type
actor_id
```

Не хранить копию всей Task.

### Activity

Добавить событие:

```text
converted_to_normal
```

---

# D3. Offline migration v3 → v4

Только `tasks-module.db`.

Никакого legacy.

Так как меняются CHECK/schema Tasks, выполнить явный offline rebuild совместимо с SQLite 3.7.17.

Требования те же, что C:

```text
BEGIN IMMEDIATE
validate frozen v3
copy own records
rebuild
restore
create Inbox table/indexes
ledger v4
validate v4
COMMIT
```

При любой ошибке:

```text
ROLLBACK
```

Исходная v3 база должна остаться без изменения.

Никаких migrations:

- при HTTP request;
- при startup;
- при открытии Orders/Products.

---

# D4. Точная семантика Microtask — 24 часа

Это важно.

При создании Microtask должен быть получен **один конкретный UTC instant**:

```text
created_at = T
micro_deadline_at = T + 24 hours
```

Разница должна быть ровно:

```text
86 400 секунд
```

Не:

- «до конца завтра»;
- «следующий календарный день»;
- `23:59`;
- UTC+3 + календарные вычисления.

Именно 24 elapsed hours.

UTC+3 используется для обычных date-only Tasks, но НЕ для расчёта 24 часов micro.

Переназначение, rename, открытие страницы и любые изменения **не перезапускают 24 часа**.

Micro overdue:

```text
micro_deadline_at < utc_now
AND status != 'done'
AND deleted_at IS NULL
```

Не хранить `overdue` в БД.

---

# D5. Создание Microtask

Отдельный endpoint:

```text
POST /api/v1/tasks-module/microtasks
```

Минимальный payload:

```json
{
  "title": "...",
  "assigned_to": 123
}
```

`assigned_to` optional.

Если не передан:

```text
assigned_to = current_user
```

Не разрешать при быстром создании:

- Project;
- deadline;
- priority;
- description;
- related entity;
- status;
- custom 24h.

Это и есть смысл Microtask.

---

# D6. Редактирование Microtask

Microtask разрешает только:

```text
title
assigned_to
status new/done
```

Плюс существующий soft delete / restore по правилам Stage B.

Не разрешать Microtask через generic PATCH превратить в скрытую «почти обычную Task»:

- project_id;
- deadline_date;
- priority;
- description;
- related reference

до conversion.

Если такие поля переданы — `422`.

Права:

**creator/admin**
- rename;
- reassign;
- complete/reopen;
- delete/restore;
- convert.

**assignee**
- rename;
- complete/reopen;
- convert;
- НЕ reassign;
- НЕ delete/restore.

То есть conversion требует существующего `can_edit`, а не только creator.

---

# D7. Microtask → Normal Task

Endpoint:

```text
POST /api/v1/tasks-module/microtasks/{id}/convert
```

Payload:

```text
version
```

Conversion должна происходить **на той же строке Task**.

Сохраняем:

- `id`;
- `title`;
- `created_by`;
- `assigned_to`;
- `created_at`;
- `completed_at`, если была done;
- `version`;
- Activity history.

Меняем:

```text
task_type = normal
micro_deadline_at = NULL
```

Если Microtask была:

```text
new → new
done → done
```

После conversion она:

- немедленно исчезает из Microtasks;
- появляется в обычной Task области или Archive, если done.

Project остаётся `NULL`.

После conversion пользователь при желании назначит обычной Task Project и date через обычный API.

Activity:

```text
converted_to_normal
```

Task mutation + Activity — одна локальная transaction.

Stale version → `409`.

---

# D8. Micro scopes

Полноценный Microtasks view должен поддерживать:

```text
my
created
team
all
```

Смысл тот же, что Stage C.

**my**
`assigned_to=current`

**created**
`created_by=current`

**team/all**
существующая Stage C visibility union.

Поскольку Microtasks не имеют Project, фактически team/all дают личные creator/assignee Tasks плюс admin global access. Это нормально — НЕ придумывать новую team-модель ради micro.

Фильтры:

```text
search
assigned_to
created_by
overdue
status
limit
offset
```

Сортировка по умолчанию:

```text
micro_deadline_at ASC
id ASC
```

То есть самые давно просроченные и ближайшие к истечению идут первыми.

---

# D9. Micro API

Минимум:

```text
GET  /microtasks
POST /microtasks

PATCH /microtasks/{id}

POST /microtasks/{id}/complete
POST /microtasks/{id}/reopen

POST /microtasks/{id}/convert
```

Soft delete/restore можно оставить через существующие Task endpoints, если permissions/type validation остаются правильными.

Не дублировать одну и ту же бизнес-логику в двух сервисах.

---

# D10. Micro summary для будущего dashboard

Нужен лёгкий endpoint:

```text
GET /microtasks/summary
```

Возвращает минимум:

```text
my_active
created_active
team_active
my_overdue
nearest_my_deadline_at
```

Это ровно то, что потребуется утверждённому блоку:

```text
Мои N
Поставленные мной N
Команда N
1 просрочена
ближайшая через ...
```

`all` отдельно в summary сейчас не нужен.

Не строить дополнительные analytics.

---

# D11. Inbox — создание события

### Новая Task

Если:

```text
created_by != assigned_to
```

то создание Task + InboxEvent выполняются **в одной Tasks transaction**.

Recipient:

```text
assigned_to
```

Actor:

```text
created_by
```

Event:

```text
task_assigned
```

Если пользователь создаёт Task себе:

**Inbox event не создавать.**

Это касается и normal, и micro.

### Reassign

Если Task переназначена:

```text
old_assignee → new_assignee
```

создать Inbox event новому assignee:

```text
task_reassigned
```

если:

```text
new_assignee != actor
```

Если пользователь переназначил Task самому себе — новый Inbox ему не нужен.

При успешном переназначении старые необработанные assignment events этой Task для прежнего assignee можно автоматически пометить handled внутри той же transaction: он больше не должен видеть в Inbox поручение, которое уже не принадлежит ему.

Это единственная автоматическая очистка Inbox, которую сейчас добавляем.

---

# D12. Inbox ≠ open Task

Открытие:

```text
GET /tasks/{id}
```

НЕ помечает Inbox прочитанным.

Mark handled — отдельное действие:

```text
POST /inbox/{event_id}/read
```

Это важно для предсказуемости.

---

# D13. Inbox API

```text
GET /inbox
GET /inbox/badge
POST /inbox/{id}/read
```

Inbox принадлежит **только recipient**.

Даже Admin не получает личный Inbox другого пользователя через обычный API.

`GET /inbox` по умолчанию:

```text
handled_at IS NULL
ORDER BY created_at DESC, id DESC
```

Soft-deleted / done Task может оставаться событием, если пользователь ещё его не разобрал. Не вводить сейчас дополнительные автоматические правила.

Badge:

```text
COUNT handled_at IS NULL
```

Лёгкий, read-only.

После Stage D dashboard `/tasks/summary`:

```text
inbox
```

больше не `null`.

Он становится количеством непрочитанных Inbox events текущего пользователя.

Остальные Stage C summary semantics не менять.

---

# D14. Notifications — без лишней инфраструктуры

Не создавать сейчас Kafka/job-system/новую общую notification platform.

Для нашего in-app toast достаточно разделить два состояния одного Inbox event:

```text
handled_at
```

= пользователь разобрал Inbox.

```text
notified_at
```

= toast уже был выдан оболочке ERP.

То есть Notification и Inbox логически разные, но могут безопасно жить на одной event record.

Это намного проще и соответствует задаче владельца.

---

# D15. Получение toast notifications

Endpoint:

```text
POST /notifications/claim
```

Только для текущего пользователя.

Он должен:

1. открыть короткую локальную Tasks transaction;
2. найти ограниченную партию Inbox events:

```text
recipient=current
notified_at IS NULL
```

3. отметить их одним `notified_at`;
4. вернуть snapshot для toast.

Например:

```json
{
  "id": 17,
  "task_id": 155,
  "task_type": "normal",
  "title": "Проверить заказ",
  "actor_id": 1
}
```

Не использовать `GET`, потому что claim меняет состояние.

Concurrent claims не должны вернуть один event дважды.

Ограничить batch, например максимум 10.

---

# D16. Notification failure

Это важная граница.

Task + Inbox фиксируются как business data.

Toast claim происходит **позже и отдельно**.

Поэтому:

- `/notifications/claim` сломан → Task остаётся;
- оболочка ERP не смогла получить toast → Task остаётся;
- Orders/Products не зависят от notification endpoint.

Если notification endpoint временно недоступен:

`notified_at` остаётся NULL.

Следующий успешный claim сможет забрать событие.

Таким образом отдельный outbox/table сейчас **не нужен**.

---

# D17. Dedupe

`task_inbox_events.dedupe_key` UNIQUE.

Ключ должен учитывать минимум:

```text
task id
event type
recipient
Task version / assignment generation
```

Повтор HTTP-response/retry не должен создать два одинаковых Inbox events.

Но реальное новое переназначение обратно тому же человеку позднее является новым событием, потому что Task version уже другая.

---

# D18. Microtasks и Inbox

Microtask, назначенная другому человеку:

- появляется у assignee в `my micro`;
- у creator в `created micro`;
- создаёт Inbox event;
- позже выдаёт toast через claim.

Self micro:

- попадает только в `my/created`;
- не создаёт шум в Inbox.

---

# D19. Collapse state

На Stage D **не создавать таблицу preference**.

Владелец просил запоминать свёрнутость блока, но это спокойно решается в Stage E через browser `localStorage` с user-specific key.

Это:
- проще;
- не нагружает backend;
- не создаёт Tasks-запрос на каждой ERP page.

Stage E обязан это реализовать.

---

# D20. Main Tasks и Microtasks не смешивать

После появления `task_type=micro`:

обычный:

```text
GET /tasks
/tasks/summary
Projects counters
Today
Overdue
Archive
```

по умолчанию относятся к:

```text
task_type='normal'
```

Microtasks управляются dedicated endpoints.

Иначе мелочь начнёт заполнять основной task manager и Archive.

Completed Microtask сохраняется в БД и доступна через explicit Microtasks `status=done`, но **не входит в основной Archive задач**.

Это соответствует выбранной UX-модели:
Microtasks — отдельный компактный инструмент.

---

# D21. Project logic

Microtask:

```text
project_id IS NULL
```

в Stage D.

Не добавлять проекты в Microtasks.

После conversion в normal Task пользователь может назначить Project обычным PATCH.

Archived Project правила Stage C не менять.

---

# D22. Transactions

Обязательные атомарные группы:

### Create assigned Task

```text
Task INSERT
Activity INSERT
InboxEvent INSERT (если нужен)
COMMIT
```

### Reassign

```text
Task CAS update
Activity
старый pending assignment Inbox close при необходимости
новый InboxEvent
COMMIT
```

### Complete/Reopen Micro

```text
Task CAS update
Activity
COMMIT
```

### Convert

```text
Task CAS update
Activity
COMMIT
```

Никаких других ERP DB внутри этих transactions.

User lookup read-only — до write transaction с повторной проверкой Task version/permissions внутри write snapshot, как уже реализовано.

---

# D23. Permissions

Не менять Stage B/C matrix.

Inbox:

```text
recipient only
```

Notification claim:

```text
recipient=current_user only
```

Microtask Task visibility:

существующая creator/assignee/admin policy.

Project access для Micro не участвует, так как Project NULL.

Не создавать специальный `micro_manager`.

---

# D24. Validation

Проверить:

- micro title;
- inactive/nonexistent assignee;
- spoof created_by;
- custom deadline попытка;
- project_id попытка;
- invalid status;
- normal-only fields;
- stale version;
- malformed time;
- duplicate Inbox events;
- invalid Inbox event ID;
- claim batch bounds.

Server timestamps клиент не задаёт.

---

# D25. Tests

Минимально обязательны.

### Migration

- clean v3→v4;
- существующие normal records сохраняются;
- Activity сохраняется;
- rollback mid-DDL;
- rollback FK;
- idempotent;
- schema corruption fail closed;
- SQLite 3.7.17.

### Micro 24h

- ровно 86 400 секунд;
- UTC instant;
- host timezone не влияет;
- rename не продлевает;
- reassign не продлевает;
- complete/reopen не меняет original deadline;
- overdue boundary до/ровно/после deadline;
- normal не имеет micro deadline;
- micro не имеет ordinary deadline/project;
- only new/done.

### Conversion

- ID сохраняется;
- creator/assignee/title/created_at сохраняются;
- history сохраняется;
- micro deadline очищается;
- stale version 409;
- Activity failure rollback;
- assignee может convert;
- бывший assignee после reassign не может;
- converted active появляется в normal list;
- done converted попадает в normal Archive.

### Inbox

- create→other создаёт 1 event;
- self assignment 0 events;
- reassign создаёт event new assignee;
- stale/retry не duplicate;
- old assignment pending закрывается при reassign away;
- only recipient reads;
- mark read;
- badge;
- Task GET не пишет handled_at;
- permissions/search не раскрывают чужой Inbox.

### Notifications

- claim возвращает pending;
- ставит notified_at;
- второй claim не возвращает тот же event;
- concurrent claims no duplicate;
- claim failure не меняет Task;
- notification failure не меняет Inbox handled state;
- Inbox остаётся после toast;
- mark Inbox read не зависит от notified state.

### Isolation

- new D operations only `tasks-module.db`;
- auth lookup read-only;
- no legacy/catalog;
- flag OFF;
- corrupted/locked Tasks DB does not affect Orders/Products;
- notification endpoint failure isolated.

### Summary

- Stage C summary fields неизменны;
- `inbox` теперь корректный count;
- normal summary не считает micro;
- project counters не считают micro.

### Runtime

Обязательный exact run:

- CentOS-compatible Linux;
- Python 3.6.8;
- SQLite 3.7.17;
- Flask/Werkzeug production versions.

---

# D26. Review flow после реализации

После D:

1. передать мне full changed production files;
2. полный diff от `8e7ae709...`;
3. tests;
4. runtime evidence;
5. пока **не commit**;
6. получить мой read-only review;
7. исправить подтверждённые issues;
8. прогнать финальные tests;
9. создать локальный Stage D commit;
10. **без нового подтверждения владельца перейти к Stage E.**

Остановиться только при CRITICAL/HIGH, требующем продуктового решения, либо при необходимости production action.

---

## Stage E после принятого D

Без отдельного согласования владельца перейти к **полному UI**, но строго в ранее утверждённом scope:

- главный экран по выбранному MASTER mockup;
- Входящие;
- Сегодня;
- Ожидаю;
- Микрозадачи;
- полный Microtasks view;
- `Мои / Поставленные мной / Команда / Все`;
- Projects;
- Project `Список / Доска`;
- короткое создание Task;
- компактная Task drawer;
- общий ERP toast + badges, загружаемые только после основной страницы;
- **Архив по отдельно утверждённому владельцем макету**;
- сохранение collapse Micro блока в `localStorage`.

Не добавлять comments/checklist/files/calendar/docs.

После review → локальный commit → автоматически Stage F.

---

## Stage F — ПОСЛЕДНИЙ ЭТАП

Это **не новый functionality stage**.

Только:

- browser E2E всех workflows;
- visual review против master mockups;
- узкая ширина ERP;
- themes/hover/focus/loading/error/empty states;
- security/IDOR/CSRF/XSS;
- SQL plans + synthetic performance;
- вернуться к `TD-TASKS-001`;
- final isolation injection;
- Tasks DB missing/corrupt/locked;
- Tasks JS failure;
- Notifications failure;
- exact production-runtime;
- clean build/test;
- screenshots:
  - Main;
  - Inbox;
  - Micro expanded/collapsed;
  - Projects;
  - Project List;
  - Project Board;
  - Task card;
  - **Archive**.

После локального Stage F commit работа останавливается.

**Никаких push/merge/deploy/production enable.**

Тогда новый Tasks считается полностью разработанным и готовым к отдельному решению владельца о внедрении.
