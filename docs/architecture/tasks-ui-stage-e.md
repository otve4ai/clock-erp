# Tasks Stage E — интерфейс нового модуля

Статус: независимый final review PASS, три MEDIUM закрыты и проверены delta-review.
Основание: принятый
Stage D `c4fb1d02596a722e7d314e3c8b537c617bcb1234`, план D → E → F.
Feature flag остаётся OFF. Push, merge, deploy и включение для пользователей не выполнялись.

## Вход и граница

Новый экран `/app/tasks-module` регистрируется fail-safe вместе с новым API.
Оболочка не читает Tasks storage: данные загружает JS после HTML.
Legacy `/app/tasks`, API и данные старого модуля не переключаются.
При включённом тестовом флаге появляется отдельная ссылка «Новые задачи»;
её Inbox badge и toast загружаются после window.load с отдельным timeout.
Ошибка optional JS/API не препятствует навигации или рендеру ERP.

## Макеты и интерфейс

Главный экран следует последнему присланному владельцем макету «Фото 1.jpg»:
пять компактных показателей, широкий блок строк микрозадач, список задач по
проектам слева, проекты и исполнители справа. Надписи соответствуют утверждённым
контрактам: «Входящие», «Поставленные мной», managerial «Ожидаю».

Навигация: Задачи, Входящие, Сегодня, Микрозадачи, Ожидаю, Проекты, Архив.
Scopes: my / created / team / all. Фильтры и поиск выполняются сервером.
Главный список сгруппирован по проектам, включая «Без проекта», с пагинацией.
Карточка справа: содержимое, ответственный, срок, проект, приоритет, статус,
история; soft-delete/restore и versioned mutations используют прежние endpoints.

Микрозадачи: quick add, собственные scopes, четыре строки preview, полный
список, сворачивание с настройкой пользователя в localStorage, обратный отсчёт,
complete/reopen/convert. Срок рассчитывается только сервером. Просрочка не
разворачивает блок автоматически. Таймер в браузере не пишет в БД.

Inbox: после GET карточка открывается сразу; отметка события прочитанным идёт
отдельно. Ошибка read оставляет карточку открытой, событие непрочитанным и показывает
сообщение. Toast не считается прочтением. Badge обновляется по inbox count.

Проекты: создание, название, участники, archive/restore. Доска только внутри
проекта, четыре колонки normal statuses. Изменение отображается после ответа API;
есть keyboard/select альтернатива drag-and-drop. Колонки подгружают по 30 строк,
с кнопкой продолжения. Ошибка или 409 не оставляет мнимого успешного перемещения.

Архив: отдельный экран завершённых normal Tasks, поиск, проект, исполнитель,
постановщик, период UTC+3, быстрые 30/90/365 дней и всё время. На широком экране
таблица, на узком — компактные строки с подписанными полями. Старые карточки
открываются с историей. Удалённые записи не входят в архив.

Изображение ранее согласованного архива (reference
`38635c8e-5e73-4924-af3e-1d0251a9cb33`) не удалось извлечь из истории ChatGPT.
Использован явно одобренный reviewer структурный fallback. Pixel-match к
недоступному изображению не заявляется.

## API / security

Единственный новый endpoint: GET `/api/v1/tasks-module/directory`, авторизованный,
no-store. Возвращает active `{id,name}`, server_now и business_date. ERP adapter
открывает auth.db через SQLite URI mode=ro; никаких email, ролей или секретов.

Task/Project ответы дополнены `permissions`, рассчитанными централизованной policy.
Это UI metadata, не новые права; API повторно проверяет каждую операцию. Права
не сохраняются в TaskActivity, их нельзя прислать в payload. Project member
без creator/assignee прав получает только чтение карточки.

Все пользовательские строки вставляются через textContent/value. CSRF используется
для каждой mutation; server version отправляется при изменении. 409 перезагружает
карточку/список и сообщает конфликт. Формы не позволяют повторный submit в полёте.
Модальное окно удерживает фокус, поддерживает Escape и возвращает фокус назад.

## Проверки

- Windows Python 3.12: 257 backend tests, 256 PASS, один POSIX lock skip.
- Linux CentOS 7.9, Python 3.6.8, SQLite 3.7.17, Flask/Werkzeug 2.0.3:
  257 PASS, 0 skips. 38 Python source hashes проверяются перед commit.
- Runtime DDL gate: PASS, 21 migration modules; schema/migrations этапом E не изменены.
- Node adapter tests: 15 checks PASS (CSRF, query encoding, safe HTTP errors,
  timeout, badge/toast, duplicate loading, debounce, offline).
- Node controller regressions: 4 PASS — read503 не блокирует карточку; отложенный
  поиск отменяется при navigation; старый reference response и старая ошибка
  не перезаписывают новые users/projects/clock/error/right-panel данные.
- Новые Python tests: policy metadata для creator/assignee/member/admin;
  отсутствие mass assignment; readonly directory; safe flag OFF;
  частичная регистрация UI/API; реальный рендер ERP и UI без Tasks connections.
- Browser: create/edit/done/history/delete/restore, Unicode/HTML as text,
  Inbox/read, micro complete/reopen/convert, project membership/archive/restore,
  board status change, readonly project member, assignee restrictions, direct-ID
  404, конфликт двух вкладок, responsive layout. Детальные screenshots и журнал
  проверки находятся в локальном `qa-reports/tasks-e-screens` вне Git.

Тесты Linux запускались от nobody в отдельном /tmp, без сети, с проверкой путей
БД. Временная папка удалена. Production остался на
`a0e53422c023bfaa8bdc40759b2f4587ea650d1c`, сервис active.

## Ограничения и следующий gate

- E не вводит SQL/schema, новые бизнес-транзакции или ERP entity links.
- Справочник ограничен 1000 активными пользователями; превышение даёт локальную
  ошибку, не тихое обрезание. При таком масштабе нужен paginated selector.
- Project selectors загружают доступные страницы проектов последовательно;
  это не N+1 COUNT. Масштабные измерения и TD-TASKS-001 относятся к этапу F.
- Специальные функциональные роли/HR/RBAC, comments/checklist/attachments/calendar
  не добавлены. Правая колонка показывает реальные имена без вымышленных метрик.
- Обычный ERP session middleware сохраняет session по прежнему контракту;
  Tasks business layer по-прежнему не пишет auth.db.
- После независимого review и локального commit следует F: финальная проверка
  UX/security/performance/isolation. Это не разрешение на deploy или включение.

## Изменённые файлы

- `app/auth.py` — узкий readonly справочник пользователей.
- `app/tasks/permissions.py`, `app/tasks/presentation.py` — серверные UI capabilities.
- `app/tasks/routes.py`, `app/tasks/ui_routes.py`, `app/tasks_boundary.py` — directory,
  HTML shell, fail-safe регистрация.
- `app/web.py`, `app/templates/_sidebar.html` — optional entry/badge после загрузки.
- `app/templates/tasks-module.html`, `app/static/css/tasks-module.css` — экран/стили.
- `app/static/js/tasks-module-api.js`, `tasks-module.js`, `tasks-module-dialogs.js`,
  `tasks-module-notifications.js` — transport, views, карточки, optional notices.
- `tests/test_tasks_ui.py`, `tests/test_tasks_core_api.py`, `tests/test_tasks_ui_js.js`,
  `tests/test_tasks_ui_state_js.js` — backend/adapters/controller regressions.
- `scripts/validate_tasks_runtime.py` — exact-runtime suite включает Stage E.
- `docs/architecture/tasks-ui-stage-e.md`, `docs/document-register.md` — отчёт/реестр.
- `docs/validation/tasks-ui-e-runtime.json`, `tasks-ui-e-ddl.json` — runtime evidence.
