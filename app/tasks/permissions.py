"""Foundation diagnostics use the existing ERP identity; no second login."""


def can_inspect_module(user):
    return bool(user and user.get("id") and user.get("role") == "admin")
