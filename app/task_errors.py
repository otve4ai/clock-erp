"""Legacy task error types, without importing task storage at ERP startup."""


class TaskValidationError(ValueError):
    def __init__(self, message, field=""):
        super().__init__(message)
        self.field = field


class TaskNotFoundError(LookupError):
    pass


class TaskConflictError(RuntimeError):
    pass


class TaskPermissionError(PermissionError):
    pass
