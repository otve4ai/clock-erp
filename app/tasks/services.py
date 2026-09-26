"""Only module diagnostics in Stage A; legacy task workflows stay untouched."""


class TasksService:
    def __init__(self, repository):
        self.repository = repository

    def status(self):
        return self.repository.status()
