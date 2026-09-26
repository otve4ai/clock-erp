"""New Tasks value validation. No Flask, auth storage or legacy task imports."""

import re
from datetime import datetime, timedelta, timezone


STATUSES = ("new", "in_progress", "waiting", "done")
PRIORITIES = ("low", "normal", "high")
EDIT_FIELDS = frozenset(("title", "description", "status", "priority", "assigned_to",
                         "deadline_date", "related_entity_type", "related_entity_id",
                         "related_entity_label"))


class TaskError(Exception):
    def __init__(self, code, message, status, fields=None):
        super().__init__(message)
        self.code, self.message, self.status, self.fields = code, message, status, fields


def invalid(field, message="Некорректное значение."):
    return TaskError("VALIDATION_ERROR", "Проверьте данные задачи.", 422, {field: message})


def not_found():
    return TaskError("TASK_NOT_FOUND", "Задача не найдена.", 404)


def conflict():
    return TaskError("VERSION_CONFLICT", "Задача уже изменена. Обновите данные.", 409)


def positive_integer(value, field):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise invalid(field, "Ожидается положительное целое число.")
    return value


def date_value(value, field="deadline_date"):
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", value):
        raise invalid(field, "Ожидается дата YYYY-MM-DD или null.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise invalid(field, "Такой даты не существует.")
    return value


def text_value(value, field, maximum, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise invalid(field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid(field, "Строка содержит некорректные символы Unicode.")
    return value.strip() if field == "title" else value


def task_values(payload, creating=False):
    if not isinstance(payload, dict):
        raise invalid("body", "Ожидается JSON object.")
    allowed = EDIT_FIELDS | ({"task_type"} if creating else {"version"})
    unknown = set(payload) - allowed
    if unknown:
        raise invalid(sorted(unknown)[0], "Поле нельзя задавать этой операцией.")
    values = {}
    for field, value in payload.items():
        if field == "version":
            continue
        if field == "task_type":
            if value != "normal":
                raise invalid(field)
        elif field in ("status", "priority"):
            if value not in (STATUSES if field == "status" else PRIORITIES):
                raise invalid(field)
        elif field == "assigned_to":
            value = positive_integer(value, field)
        elif field == "deadline_date":
            value = date_value(value)
        else:
            maximum = {"title": 500, "description": 50000, "related_entity_type": 64,
                       "related_entity_id": 200, "related_entity_label": 500}[field]
            value = text_value(value, field, maximum, field.startswith("related_"))
            if field == "title" and not value:
                raise invalid(field, "Название обязательно.")
        values[field] = value
    if creating and "title" not in values:
        raise invalid("title", "Название обязательно.")
    if not creating and not values:
        raise invalid("body", "Нет полей для изменения.")
    return values


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def business_today():
    # Date-only deadlines never become timestamps; only the business day's
    # boundary follows the ERP Moscow calendar, not the host's timezone.
    return datetime.now(timezone(timedelta(hours=3))).date().isoformat()


def list_options(options):
    allowed = {"scope", "status", "priority", "deadline_date", "overdue", "today",
               "search", "limit", "offset"}
    if set(options) - allowed:
        raise invalid(sorted(set(options) - allowed)[0], "Неизвестный фильтр.")
    result = dict(options)
    for field, choices in (("status", STATUSES), ("priority", PRIORITIES)):
        if field in result and result[field] not in choices:
            raise invalid(field)
    if "deadline_date" in result:
        result["deadline_date"] = date_value(result["deadline_date"])
    for field in ("overdue", "today"):
        if field in result:
            if result[field] not in ("true", "false", "1", "0"):
                raise invalid(field)
            result[field] = result[field] in ("true", "1")
    if "search" in result:
        result["search"] = text_value(result["search"], "search", 500).casefold()
    for field, default, maximum in (("limit", 50, 100), ("offset", 0, 1000000)):
        raw = str(result.get(field, default))
        if not re.match(r"^[0-9]{1,7}$", raw):
            raise invalid(field)
        number = int(raw)
        if number > maximum or number < (1 if field == "limit" else 0):
            raise invalid(field)
        result[field] = number
    return result
