"""New Tasks value validation. No Flask, auth storage or legacy task imports."""

import re
from datetime import datetime, timedelta, timezone


STATUSES = ("new", "in_progress", "waiting", "done")
PRIORITIES = ("low", "normal", "high")
EDIT_FIELDS = frozenset(("title", "description", "status", "priority", "assigned_to",
                         "deadline_date", "related_entity_type", "related_entity_id",
                         "related_entity_label", "project_id"))
BUSINESS_TIMEZONE = timezone(timedelta(hours=3))


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
        elif field == "project_id":
            value = None if value is None else positive_integer(value, field)
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


def utc_instant(value):
    """Parse our server UTC clock without Python 3.7's fromisoformat API."""
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00", value):
        raise ValueError("Invalid server UTC clock")
    raw = value[:-6]
    pattern = "%Y-%m-%dT%H:%M:%S.%f" if "." in raw else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)


def micro_values(payload, creating=False):
    allowed = {"title", "assigned_to"} if creating else {"title", "assigned_to", "status", "version"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise invalid("body", "У микрозадачи доступны только название, исполнитель и завершение.")
    if "status" in payload and payload["status"] not in ("new", "done"):
        raise invalid("status")
    return task_values(payload, creating=creating)


def micro_options(options):
    if set(options) - {"scope", "search", "assigned_to", "created_by", "overdue", "status", "limit", "offset"}:
        raise invalid("query")
    result = list_options(options)
    if "status" in result and result["status"] not in ("new", "done"):
        raise invalid("status")
    return result


def business_today():
    # Date-only deadlines never become timestamps; only the business day's
    # boundary follows the ERP Moscow calendar, not the host's timezone.
    return datetime.now(BUSINESS_TIMEZONE).date().isoformat()


def list_options(options):
    allowed = {"scope", "status", "priority", "deadline_date", "overdue", "today",
               "search", "limit", "offset", "view", "project_id", "project",
               "assigned_to", "created_by", "date_from", "date_to"}
    if set(options) - allowed:
        raise invalid(sorted(set(options) - allowed)[0], "Неизвестный фильтр.")
    result = dict(options)
    if "view" in result and result["view"] not in ("today", "overdue", "delegated_waiting", "archive"):
        raise invalid("view")
    if "project" in result and (result["project"] != "none" or "project_id" in result):
        raise invalid("project")
    for field in ("project_id", "assigned_to", "created_by"):
        if field in result:
            raw = str(result[field])
            if not re.fullmatch(r"[0-9]{1,19}", raw):
                raise invalid(field)
            result[field] = positive_integer(int(raw), field)
    for field in ("date_from", "date_to"):
        if field in result:
            if result.get("view") != "archive" or result[field] is None:
                raise invalid(field, "Период применим только к архиву.")
            date_value(result[field], field)
    if result.get("date_from", "0001-01-01") > result.get("date_to", "9999-12-31"):
        raise invalid("date_to")
    for field in ("date_from", "date_to"):
        if field in result:
            try:
                instant = datetime.strptime(result[field], "%Y-%m-%d").replace(tzinfo=BUSINESS_TIMEZONE)
                if field == "date_to":
                    instant += timedelta(days=1)
                result[field] = instant.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                raise invalid(field)
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


def project_name(value):
    value = text_value(value, "name", 200).strip()
    if not value:
        raise invalid("name", "Название обязательно.")
    return value


def project_options(options):
    if set(options) - {"archived", "search", "limit", "offset"}:
        raise invalid("query")
    result = list_options({key: value for key, value in options.items() if key != "archived"})
    archived = options.get("archived", "false")
    if archived not in ("true", "false", "1", "0"):
        raise invalid("archived")
    result["archived"] = archived in ("true", "1")
    return result
