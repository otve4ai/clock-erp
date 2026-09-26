"""Microtask selections; visibility predicates come from the shared policy."""


class MicroQueries:
    @staticmethod
    def _micro_where(scope, options, now):
        visibility, parameters = scope["visibility"]
        clauses, parameters = ["task_type='micro'", "deleted_at IS NULL", visibility], list(parameters)
        for source in (scope, options):
            for field in ("assigned_to", "created_by"):
                if field in source:
                    clauses.append(field + "=?")
                    parameters.append(source[field])
        clauses.append("status=?")
        parameters.append(options.get("status", "new"))
        if "overdue" in options:
            clauses.append("(micro_deadline_at < ? AND status!='done')=" + ("1" if options["overdue"] else "0"))
            parameters.append(now)
        if options.get("search"):
            clauses.append("instr(tasks_casefold(title),?)>0")
            parameters.append(options["search"])
        return " AND ".join(clauses), parameters

    def micros(self, scope, options, now):
        where, parameters = self._micro_where(scope, options, now)
        total = self.connection.execute("SELECT COUNT(*) FROM tasks WHERE " + where, parameters).fetchone()[0]
        rows = self.connection.execute("SELECT * FROM tasks WHERE " + where +
                                       " ORDER BY micro_deadline_at,id LIMIT ? OFFSET ?",
                                       parameters + [options["limit"], options["offset"]]).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "limit": options["limit"], "offset": options["offset"]}

    def micro_summary(self, scopes, now):
        result = {}
        # Three fixed aggregate queries, independent of task/project count.
        for name, scope in scopes.items():
            where, parameters = self._micro_where(scope, {}, now)
            row = self.connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(micro_deadline_at < ?),0),"
                "MIN(CASE WHEN micro_deadline_at >= ? THEN micro_deadline_at END) "
                "FROM tasks WHERE " + where, [now, now] + parameters).fetchone()
            result[name + "_active"] = row[0]
            if name == "my":
                result["my_overdue"], result["nearest_my_deadline_at"] = row[1], row[2]
        return result
