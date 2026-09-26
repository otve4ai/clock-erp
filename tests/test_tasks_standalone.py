import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TasksStandaloneUiTest(unittest.TestCase):
    def test_task_creation_is_available_only_inside_tasks_module(self):
        allowed = {
            PROJECT_ROOT / "app" / "templates" / "tasks.html",
            PROJECT_ROOT / "app" / "static" / "js" / "tasks.js",
        }
        roots = (
            PROJECT_ROOT / "app" / "templates",
            PROJECT_ROOT / "app" / "static" / "js",
        )
        forbidden = (
            "Создать задачу",
            "Поставить задачу",
            "Связанные задачи",
            "data-entity-tasks",
            "VechasuEntityTasksInit",
            "/api/v1/tasks/by-entity",
            "/api/v1/tasks/entities",
            "/app/tasks?entity_type=",
        )
        violations = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or path in allowed:
                    continue
                if path.suffix not in {".html", ".js"}:
                    continue
                source = path.read_text(encoding="utf-8")
                for marker in forbidden:
                    if marker in source:
                        violations.append("{}: {}".format(path.relative_to(PROJECT_ROOT), marker))
        self.assertEqual(violations, [])

    def test_cross_module_task_assets_are_removed(self):
        self.assertFalse((PROJECT_ROOT / "app/static/js/entity-tasks.js").exists())
        self.assertFalse((PROJECT_ROOT / "app/static/css/entity-tasks.css").exists())


if __name__ == "__main__":
    unittest.main()
