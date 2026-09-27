"""Inbox and toast delivery remain separate from task business mutations."""

from .domain import invalid, list_options, positive_integer, utc_now
from .permissions import require_actor


class InboxService:
    def __init__(self, repository, now=utc_now):
        self.repository, self.now = repository, now

    def list(self, user, options=None, badge=False):
        require_actor(user)
        options = options or {}
        if set(options) - (set() if badge else {"limit", "offset"}):
            raise invalid("query")
        paging = list_options(options)
        with self.repository.transaction() as session:
            if badge:
                return session.inbox_counts(user["id"])
            return session.inbox(user["id"], paging["limit"], paging["offset"])

    def read(self, user, event_id, payload):
        require_actor(user)
        positive_integer(event_id, "id")
        if not isinstance(payload, dict) or payload:
            raise invalid("body")
        with self.repository.transaction(write=True) as session:
            return session.read_inbox(user["id"], event_id, self.now())

    def claim(self, user, payload):
        require_actor(user)
        if not isinstance(payload, dict) or set(payload) - {"limit"}:
            raise invalid("body")
        limit = positive_integer(payload.get("limit", 10), "limit")
        if limit > 10:
            raise invalid("limit")
        with self.repository.transaction(write=True) as session:
            return session.claim_notifications(user["id"], self.now(), limit)
