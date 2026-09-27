"""Project SQL only, on the Tasks transaction supplied by the repository."""

import json

from .domain import TaskError


def project_conflict():
    return TaskError("VERSION_CONFLICT", "Проект уже изменён. Обновите данные.", 409)


class ProjectQueries:
    def get_project(self, project_id, visibility):
        clause, parameters = visibility
        row = self.connection.execute("SELECT * FROM task_projects WHERE id=? AND " + clause,
                                      [project_id] + parameters).fetchone()
        return dict(row) if row is not None else None

    def create_project(self, name, owner, timestamp):
        cursor = self.connection.execute(
            "INSERT INTO task_projects(name,owner_id,created_at,updated_at) VALUES(?,?,?,?)",
            (name, owner, timestamp, timestamp))
        return self.get_project(cursor.lastrowid, ("1=1", []))

    def update_project(self, project_id, version, values):
        if set(values) - {"name", "archived_at", "updated_at"}:
            raise ValueError("Invalid project update fields")
        fields = sorted(values)
        cursor = self.connection.execute(
            "UPDATE task_projects SET " + ",".join(field + "=?" for field in fields) +
            ",version=version+1 WHERE id=? AND version=?",
            [values[field] for field in fields] + [project_id, version])
        if cursor.rowcount != 1:
            raise project_conflict()
        return self.get_project(project_id, ("1=1", []))

    def has_member(self, project_id, user_id):
        return self.connection.execute("SELECT 1 FROM task_project_members WHERE project_id=? AND user_id=?",
                                       (project_id, user_id)).fetchone() is not None

    def add_member(self, project_id, user_id, timestamp):
        self.connection.execute("INSERT INTO task_project_members(project_id,user_id,created_at) VALUES(?,?,?)",
                                (project_id, user_id, timestamp))

    def remove_member(self, project_id, user_id):
        self.connection.execute("DELETE FROM task_project_members WHERE project_id=? AND user_id=?", (project_id, user_id))

    def members(self, project_id, limit, offset):
        total = self.connection.execute("SELECT COUNT(*) FROM task_project_members WHERE project_id=?", (project_id,)).fetchone()[0]
        rows = self.connection.execute("SELECT * FROM task_project_members WHERE project_id=? ORDER BY user_id LIMIT ? OFFSET ?",
                                       (project_id, limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def add_project_activity(self, project, actor, event, timestamp, payload):
        self.connection.execute(
            "INSERT INTO task_project_activity(project_id,actor,event_type,timestamp,project_version,payload) VALUES(?,?,?,?,?,?)",
            (project["id"], actor, event, timestamp, project["version"], json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def project_activity(self, project_id, limit, offset):
        rows = self.connection.execute("SELECT * FROM task_project_activity WHERE project_id=? ORDER BY id LIMIT ? OFFSET ?",
                                       (project_id, limit, offset)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result

    @staticmethod
    def _counter_sql():
        return ("COALESCE(SUM(status!='done'),0) AS open,"
                "COALESCE(SUM(status='in_progress'),0) AS in_progress,"
                "COALESCE(SUM(status!='done' AND deadline_date < ?),0) AS overdue")

    def project_counters(self, project_id, today):
        row = self.connection.execute("SELECT " + self._counter_sql() +
                                      " FROM tasks WHERE project_id=? AND deleted_at IS NULL", (today, project_id)).fetchone()
        return dict(row)

    def projects(self, visibility, options, today):
        clause, parameters = visibility
        where = clause + " AND archived_at IS " + ("NOT NULL" if options["archived"] else "NULL")
        parameters = list(parameters)
        if options.get("search"):
            # instr treats %, _ and backslash literally, without extra SQL syntax.
            where += " AND instr(tasks_casefold(name),?)>0"
            parameters.append(options["search"])
        total = self.connection.execute("SELECT COUNT(*) FROM task_projects WHERE " + where, parameters).fetchone()[0]
        # Aggregate only the selected page, not one COUNT per project or all
        # projects' tasks. SQLite 3.7 supports derived tables and GROUP BY.
        rows = self.connection.execute(
            "SELECT selected.*,COUNT(CASE WHEN t.status!='done' THEN 1 END) AS open,"
            "COUNT(CASE WHEN t.status='in_progress' THEN 1 END) AS in_progress,"
            "COUNT(CASE WHEN t.status!='done' AND t.deadline_date < ? THEN 1 END) AS overdue "
            "FROM (SELECT * FROM task_projects WHERE " + where + " ORDER BY id LIMIT ? OFFSET ?) selected "
            "LEFT JOIN tasks t ON t.project_id=selected.id AND t.deleted_at IS NULL "
            "GROUP BY selected.id ORDER BY selected.id",
            [today] + parameters + [options["limit"], options["offset"]]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["counters"] = {key: item.pop(key) for key in ("open", "in_progress", "overdue")}
            items.append(item)
        return {"items": items, "total": total, "limit": options["limit"], "offset": options["offset"]}
