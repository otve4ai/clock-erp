"""Reparse saved mail from verified IMAP originals without replacing threads."""

from app.services.mail import MailSynchronizer, parse_message


def repair_messages(store, client, apply=False, after_id=0, limit=200):
    account = store.account(include_disabled=False)
    if not account:
        return {"checked": 0, "changed": 0, "skipped": 0, "last_id": after_id}
    with store.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM mail_messages WHERE account_id=? AND id>? "
            "AND remote_uid>0 AND html_body<>'' ORDER BY id LIMIT ?",
            (account["id"], after_id, max(1, min(limit, 1000))),
        )]
    result = {"checked": 0, "changed": 0, "skipped": 0, "last_id": after_id}
    for row in rows:
        result["checked"] += 1
        result["last_id"] = row["id"]
        status, unused = client.select(row["remote_folder"], readonly=True)
        if status != "OK" or MailSynchronizer._uidvalidity(client.response("UIDVALIDITY")[1]) != row["uidvalidity"]:
            result["skipped"] += 1
            continue
        status, fetched = client.uid("fetch", str(row["remote_uid"]), "(BODY.PEEK[])")
        raw = next((part[1] for part in fetched or [] if isinstance(part, tuple)), None)
        if status != "OK" or not raw:
            result["skipped"] += 1
            continue
        parsed = parse_message(raw)
        if parsed["message_id"] != row["message_id"] or not parsed["html_body"]:
            result["skipped"] += 1
            continue
        fields = ("html_body", "text_body", "snippet", "external_images")
        values = tuple(parsed[key] for key in fields)
        if values == tuple(row[key] for key in fields):
            continue
        if apply:
            with store.connect() as connection:
                # Update presentation only, retaining IDs, read state, links and attachments.
                connection.execute(
                    "UPDATE mail_messages SET html_body=?,text_body=?,snippet=?,external_images=? WHERE id=?",
                    values + (row["id"],),
                )
                latest = connection.execute(
                    "SELECT id FROM mail_messages WHERE thread_id=? ORDER BY sent_at DESC,id DESC LIMIT 1",
                    (row["thread_id"],),
                ).fetchone()
                if latest and latest[0] == row["id"]:
                    connection.execute("UPDATE mail_threads SET last_snippet=? WHERE id=?",
                                       (parsed["snippet"], row["thread_id"]))
                connection.commit()
        result["changed"] += 1
    return result
