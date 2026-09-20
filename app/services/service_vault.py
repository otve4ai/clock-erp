"""Encrypted catalogue of company services and per-user access rules."""

import base64
import binascii
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken


BUILTIN_ICONS = {"globe", "cart", "truck", "server", "cloud", "lock"}
PERMISSIONS = (
    "can_view", "can_open", "can_view_login", "can_copy_login",
    "can_view_password", "can_copy_password", "can_edit",
    "can_manage_access", "can_archive",
)
MAX_ICON_BYTES = 512 * 1024
BASE_MIGRATION_ID = "2026-08-28-services-vault-v1"
MIGRATION_ID = "2026-09-20-service-categories-v2"


class ServiceVaultError(ValueError):
    pass


class ServiceNotFoundError(ServiceVaultError):
    pass


class ServicePermissionError(ServiceVaultError):
    pass


class ServiceConflictError(ServiceVaultError):
    pass


class VaultKeyError(RuntimeError):
    pass


def validate_service_url(value):
    value = str(value or "").strip()
    if len(value) > 2048:
        raise ServiceVaultError("Ссылка слишком длинная")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ServiceVaultError("Разрешены только ссылки http:// и https://")
    if parsed.username or parsed.password:
        raise ServiceVaultError("Ссылка не должна содержать логин или пароль")
    if any(ord(character) < 32 for character in value):
        raise ServiceVaultError("Ссылка содержит недопустимые символы")
    return value


def validate_icon(content, declared_mime):
    content = bytes(content or b"")
    if not content or len(content) > MAX_ICON_BYTES:
        raise ServiceVaultError("Иконка должна быть не больше 512 КБ")
    mime = ""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        mime = "image/webp"
    if not mime or declared_mime not in {mime, "application/octet-stream", ""}:
        raise ServiceVaultError("Поддерживаются только PNG, JPEG и WEBP")
    return content, mime


class ServiceVault:
    def __init__(self, path, key=None):
        self.path = Path(path)
        raw_key = str(key if key is not None else os.getenv("SERVICE_VAULT_KEY", "")).strip()
        try:
            decoded = base64.urlsafe_b64decode(raw_key.encode("ascii"))
            if len(decoded) != 32:
                raise ValueError
            self.cipher = Fernet(raw_key.encode("ascii"))
        except (ValueError, TypeError, binascii.Error):
            raise VaultKeyError("SERVICE_VAULT_KEY отсутствует или имеет неверный формат")
        self.validate_schema()

    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def validate_schema(self):
        if not self.path.exists():
            raise ServiceVaultError("Требуется миграция базы сервисов")
        with self.connect() as connection:
            ledger = {row[0] for row in connection.execute(
                "SELECT migration_id FROM service_schema_migrations"
            )}
            if ledger != {BASE_MIGRATION_ID, MIGRATION_ID}:
                raise ServiceVaultError("Требуется миграция базы сервисов")

    def list_categories(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT c.category_key AS key,c.name,c.position,COUNT(s.id) service_count "
                "FROM service_categories c LEFT JOIN services s ON s.category=c.category_key "
                "GROUP BY c.category_key,c.name,c.position ORDER BY c.position,c.name"
            ).fetchall()]

    @staticmethod
    def _category_name(value):
        name = " ".join(str(value or "").split())
        if not name or len(name) > 60:
            raise ServiceVaultError("Название раздела должно содержать от 1 до 60 символов")
        return name

    def create_category(self, name, user):
        if not self._is_owner(user):
            raise ServicePermissionError("Недостаточно прав")
        name = self._category_name(name)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM service_categories WHERE name=? COLLATE NOCASE", (name,)
            ).fetchone():
                raise ServiceConflictError("Раздел с таким названием уже существует")
            category_key = "custom-" + secrets.token_hex(6)
            position = connection.execute(
                "SELECT COALESCE(MAX(position),0)+10 FROM service_categories"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO service_categories(category_key,name,position,created_at,updated_at) "
                "VALUES(?,?,?,?,?)", (category_key, name, position, now, now)
            )
            connection.commit()
        return category_key

    def rename_category(self, category_key, name, user):
        if not self._is_owner(user):
            raise ServicePermissionError("Недостаточно прав")
        name = self._category_name(name)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT category_key FROM service_categories WHERE category_key=?", (category_key,)
            ).fetchone()
            if row is None:
                raise ServiceNotFoundError("Раздел не найден")
            duplicate = connection.execute(
                "SELECT 1 FROM service_categories WHERE name=? COLLATE NOCASE AND category_key<>?",
                (name, category_key),
            ).fetchone()
            if duplicate:
                raise ServiceConflictError("Раздел с таким названием уже существует")
            connection.execute(
                "UPDATE service_categories SET name=?,updated_at=? WHERE category_key=?",
                (name, int(time.time()), category_key),
            )
            connection.commit()

    def reorder_categories(self, ordered_keys, user):
        if not self._is_owner(user):
            raise ServicePermissionError("Недостаточно прав")
        ordered_keys = [str(value) for value in ordered_keys]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = [row[0] for row in connection.execute(
                "SELECT category_key FROM service_categories ORDER BY position,name"
            )]
            if len(ordered_keys) != len(set(ordered_keys)) or set(ordered_keys) != set(existing):
                raise ServiceVaultError("Некорректный порядок разделов")
            now = int(time.time())
            for position, category_key in enumerate(ordered_keys, 1):
                connection.execute(
                    "UPDATE service_categories SET position=?,updated_at=? WHERE category_key=?",
                    (position * 10, now, category_key),
                )
            connection.commit()

    def delete_category(self, category_key, replacement_key, user):
        if not self._is_owner(user):
            raise ServicePermissionError("Недостаточно прав")
        replacement_key = str(replacement_key or "")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            category = connection.execute(
                "SELECT category_key,name FROM service_categories WHERE category_key=?", (category_key,)
            ).fetchone()
            if category is None:
                raise ServiceNotFoundError("Раздел не найден")
            if connection.execute("SELECT COUNT(*) FROM service_categories").fetchone()[0] <= 1:
                raise ServiceVaultError("Нельзя удалить единственный раздел")
            service_count = connection.execute(
                "SELECT COUNT(*) FROM services WHERE category=?", (category_key,)
            ).fetchone()[0]
            if service_count:
                if not replacement_key or replacement_key == category_key:
                    raise ServiceVaultError("Выберите раздел для переноса сервисов")
                if connection.execute(
                    "SELECT 1 FROM service_categories WHERE category_key=?", (replacement_key,)
                ).fetchone() is None:
                    raise ServiceVaultError("Раздел для переноса не найден")
                connection.execute(
                    "UPDATE services SET category=?,updated_at=?,version=version+1 WHERE category=?",
                    (replacement_key, int(time.time()), category_key),
                )
            connection.execute(
                "DELETE FROM service_categories WHERE category_key=?", (category_key,)
            )
            connection.commit()
            return {"key": category["category_key"], "name": category["name"], "moved": service_count}

    def encrypt(self, value):
        value = str(value or "")
        return self.cipher.encrypt(value.encode("utf-8")) if value else None

    def decrypt(self, value):
        if value is None:
            return ""
        try:
            return self.cipher.decrypt(bytes(value)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, TypeError) as error:
            raise VaultKeyError("Не удалось расшифровать реквизиты") from error

    @staticmethod
    def _is_owner(user):
        return str((user or {}).get("role") or "") == "admin"

    def _permission(self, connection, service_id, user):
        if self._is_owner(user):
            return {name: True for name in PERMISSIONS}
        row = connection.execute(
            "SELECT {} FROM service_permissions WHERE service_id=? AND user_id=?".format(
                ",".join(PERMISSIONS)
            ),
            (int(service_id), int((user or {}).get("id") or 0)),
        ).fetchone()
        rights = {name: bool(row[name]) if row else False for name in PERMISSIONS}
        # Every more specific permission is subordinate to visibility of the
        # service itself.  Besides keeping the UI consistent, this closes the
        # direct credential endpoints when an old or malformed grant contains
        # (for example) can_view_password=1 and can_view=0.
        if not rights["can_view"]:
            return {name: False for name in PERMISSIONS}
        return rights

    def access_report(self, user_id, actor, target_is_owner=False):
        """Return services known to a user and credentials worth rotating."""
        if not self._is_owner(actor):
            raise ServicePermissionError("Недостаточно прав")
        user_id = int(user_id)
        with self.connect() as connection:
            if target_is_owner:
                rows = connection.execute(
                    "SELECT s.id,s.name,1 can_view_password,1 can_copy_password "
                    "FROM services s ORDER BY s.name,s.id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT s.id,s.name,p.can_view_password,p.can_copy_password "
                    "FROM service_permissions p JOIN services s ON s.id=p.service_id "
                    "WHERE p.user_id=? AND (p.can_view=1 OR p.can_open=1 "
                    "OR p.can_view_login=1 OR p.can_copy_login=1 "
                    "OR p.can_view_password=1 OR p.can_copy_password=1 "
                    "OR p.can_edit=1 OR p.can_manage_access=1 OR p.can_archive=1) "
                    "ORDER BY s.name,s.id",
                    (user_id,),
                ).fetchall()
            services = []
            passwords = []
            for row in rows:
                item = {"id": int(row["id"]), "name": row["name"]}
                services.append(item)
                if row["can_view_password"] or row["can_copy_password"]:
                    accounts = connection.execute(
                        "SELECT id,label FROM service_accounts "
                        "WHERE service_id=? AND password_encrypted IS NOT NULL "
                        "ORDER BY position,id",
                        (row["id"],),
                    ).fetchall()
                    passwords.extend({
                        "service_id": int(row["id"]),
                        "service_name": row["name"],
                        "account_id": int(account["id"]),
                        "account_label": account["label"],
                    } for account in accounts)
            return {"user_id": user_id, "services": services, "passwords": passwords}

    def revoke_user(self, user_id, actor, target_is_owner=False):
        """Remove every service grant and preference for one employee."""
        report = self.access_report(user_id, actor, target_is_owner)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM service_permissions WHERE user_id=?", (int(user_id),)
            )
            connection.execute(
                "DELETE FROM service_user_preferences WHERE user_id=?", (int(user_id),)
            )
            connection.commit()
        return report

    def require(self, connection, service_id, user, permission, allow_archived=False):
        service = connection.execute(
            "SELECT * FROM services WHERE id=?", (int(service_id),)
        ).fetchone()
        if service is None:
            raise ServiceNotFoundError("Сервис не найден")
        if service["archived_at"] and not allow_archived:
            raise ServiceNotFoundError("Сервис находится в архиве")
        rights = self._permission(connection, service_id, user)
        if not rights.get(permission):
            raise ServicePermissionError("Недостаточно прав")
        return service, rights

    def list_services(self, user, archived=False):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT s.*, COALESCE(p.favorite,0) favorite, COALESCE(p.sort_order,0) sort_order "
                "FROM services s LEFT JOIN service_user_preferences p "
                "ON p.service_id=s.id AND p.user_id=? WHERE "
                + ("s.archived_at IS NOT NULL " if archived else "s.archived_at IS NULL ")
                + "ORDER BY favorite DESC, sort_order, s.id",
                (int(user["id"]),),
            ).fetchall()
            result = []
            for row in rows:
                rights = self._permission(connection, row["id"], user)
                if not rights["can_view"] or (archived and not self._is_owner(user)):
                    continue
                accounts = connection.execute(
                    "SELECT id,label,login_encrypted IS NOT NULL has_login,"
                    "password_encrypted IS NOT NULL has_password FROM service_accounts "
                    "WHERE service_id=? ORDER BY position,id", (row["id"],)
                ).fetchall()
                grants = []
                if rights["can_manage_access"]:
                    grants = [dict(item) for item in connection.execute(
                        "SELECT user_id,{} FROM service_permissions WHERE service_id=? ORDER BY user_id".format(
                            ",".join(PERMISSIONS)
                        ), (row["id"],)
                    ).fetchall()]
                result.append({
                    "id": row["id"], "name": row["name"],
                    "url": row["url"] if (rights["can_open"] or rights["can_edit"]) else "",
                    "domain": urlsplit(row["url"]).netloc, "description": row["description"],
                    "category": row["category"], "icon": row["icon"],
                    "has_custom_icon": bool(row["icon_blob"]), "favorite": bool(row["favorite"]),
                    "sort_order": row["sort_order"], "version": row["version"],
                    "archived": bool(row["archived_at"]), "permissions": rights,
                    "accounts": [dict(account) for account in accounts],
                    "grants": grants,
                })
            return result

    def create(self, payload, user, icon=None):
        if not self._is_owner(user):
            raise ServicePermissionError("Недостаточно прав")
        normalized = self._validated_payload(payload)
        now = int(time.time())
        icon_blob, icon_mime = icon or (None, None)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO services(name,url,description,category,icon,icon_blob,icon_mime,"
                "created_by,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (normalized["name"], normalized["url"], normalized["description"],
                 normalized["category"], normalized["icon"], icon_blob, icon_mime,
                 int(user["id"]), now, now),
            )
            service_id = cursor.lastrowid
            self._replace_accounts(connection, service_id, normalized["accounts"], now)
            self._replace_permissions(connection, service_id, normalized["permissions"])
            connection.execute(
                "INSERT INTO service_user_preferences(service_id,user_id,favorite,sort_order,version,updated_at) "
                "VALUES(?,?,?,?,1,?)", (service_id, int(user["id"]), int(normalized["favorite"]), service_id, now)
            )
            connection.commit()
        return service_id

    def update(self, service_id, payload, user, icon=None):
        normalized = self._validated_payload(payload)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            service, rights = self.require(connection, service_id, user, "can_edit")
            expected = int(payload.get("version") or service["version"])
            icon_sql = ""
            parameters = [normalized["name"], normalized["url"], normalized["description"], normalized["category"], normalized["icon"]]
            if icon:
                icon_sql = ",icon_blob=?,icon_mime=?"
                parameters.extend(icon)
            parameters.extend([now, int(service_id), expected])
            cursor = connection.execute(
                "UPDATE services SET name=?,url=?,description=?,category=?,icon=?{}"
                ",updated_at=?,version=version+1 WHERE id=? AND version=?".format(icon_sql), parameters
            )
            if cursor.rowcount != 1:
                raise ServiceConflictError("Сервис уже изменён другим пользователем")
            self._replace_accounts(connection, service_id, normalized["accounts"], now)
            if normalized["permissions"] is not None:
                if not rights["can_manage_access"]:
                    raise ServicePermissionError("Недостаточно прав для управления доступами")
                self._replace_permissions(connection, service_id, normalized["permissions"])
            connection.commit()

    def _validated_payload(self, payload):
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 160:
            raise ServiceVaultError("Укажите название сервиса")
        category = str(payload.get("category") or "")
        with self.connect() as connection:
            if not category:
                row = connection.execute(
                    "SELECT category_key FROM service_categories ORDER BY position,name LIMIT 1"
                ).fetchone()
                category = row[0] if row else ""
            if not category or connection.execute(
                "SELECT 1 FROM service_categories WHERE category_key=?", (category,)
            ).fetchone() is None:
                raise ServiceVaultError("Неизвестный раздел")
        icon = str(payload.get("icon") or "globe")
        if icon not in BUILTIN_ICONS:
            icon = "globe"
        accounts = payload.get("accounts") or []
        if not isinstance(accounts, list) or len(accounts) > 20:
            raise ServiceVaultError("Некорректный список аккаунтов")
        clean_accounts = []
        for account in accounts:
            label = str(account.get("label") or "Основной аккаунт").strip()[:120]
            login = str(account.get("login") or "")
            password = str(account.get("password") or "")
            if len(login) > 1000 or len(password) > 4000:
                raise ServiceVaultError("Реквизиты слишком длинные")
            if login or password or label:
                clean_accounts.append({
                    "id": int(account.get("id") or 0),
                    "label": label or "Основной аккаунт", "login": login,
                    "password": password,
                })
        permissions = payload.get("permissions")
        if permissions is not None and not isinstance(permissions, list):
            raise ServiceVaultError("Некорректные права доступа")
        return {
            "name": name, "url": validate_service_url(payload.get("url")),
            "description": str(payload.get("description") or "").strip()[:1000],
            "category": category, "icon": icon, "accounts": clean_accounts,
            "permissions": permissions, "favorite": bool(payload.get("favorite")),
        }

    def _replace_accounts(self, connection, service_id, accounts, now):
        existing = {row["id"]: row for row in connection.execute(
            "SELECT * FROM service_accounts WHERE service_id=?", (service_id,)
        ).fetchall()}
        retained = []
        for position, account in enumerate(accounts):
            account_id = int(account.get("id") or 0)
            old = existing.get(account_id)
            login_blob = self.encrypt(account["login"]) if account["login"] else (old["login_encrypted"] if old else None)
            password_blob = self.encrypt(account["password"]) if account["password"] else (old["password_encrypted"] if old else None)
            if old:
                connection.execute(
                    "UPDATE service_accounts SET label=?,login_encrypted=?,password_encrypted=?,position=?,updated_at=? WHERE id=?",
                    (account["label"], login_blob, password_blob, position, now, account_id),
                )
                retained.append(account_id)
            else:
                cursor = connection.execute(
                    "INSERT INTO service_accounts(service_id,label,login_encrypted,password_encrypted,position,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)", (service_id, account["label"], login_blob, password_blob, position, now, now)
                )
                retained.append(cursor.lastrowid)
        if retained:
            placeholders = ",".join("?" for _ in retained)
            connection.execute(
                "DELETE FROM service_accounts WHERE service_id=? AND id NOT IN ({})".format(placeholders),
                [service_id] + retained,
            )
        else:
            connection.execute("DELETE FROM service_accounts WHERE service_id=?", (service_id,))

    def _replace_permissions(self, connection, service_id, permissions):
        if permissions is None:
            return
        connection.execute("DELETE FROM service_permissions WHERE service_id=?", (service_id,))
        for item in permissions:
            if not isinstance(item, dict):
                raise ServiceVaultError("Некорректные права доступа")
            user_id = int(item.get("user_id") or 0)
            if not user_id:
                continue
            can_view = bool(item.get("can_view"))
            values = [int(can_view and bool(item.get(name))) for name in PERMISSIONS]
            connection.execute(
                "INSERT INTO service_permissions(service_id,user_id,{}) VALUES(?,?,{})".format(
                    ",".join(PERMISSIONS), ",".join("?" for _ in PERMISSIONS)
                ), [service_id, user_id] + values,
            )

    def credential(self, account_id, user, kind, for_copy=False):
        permission = ("can_copy_" if for_copy else "can_view_") + kind
        column = "password_encrypted" if kind == "password" else "login_encrypted"
        with self.connect() as connection:
            account = connection.execute("SELECT * FROM service_accounts WHERE id=?", (int(account_id),)).fetchone()
            if account is None:
                raise ServiceNotFoundError("Аккаунт не найден")
            service, _rights = self.require(connection, account["service_id"], user, permission)
            return self.decrypt(account[column]), dict(service), dict(account)

    def set_archived(self, service_id, archived, user):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.require(connection, service_id, user, "can_archive", allow_archived=True)
            connection.execute(
                "UPDATE services SET archived_at=?,updated_at=?,version=version+1 WHERE id=?",
                (int(time.time()) if archived else None, int(time.time()), int(service_id)),
            )
            connection.commit()

    def delete_archived(self, service_id, user):
        """Permanently delete an archived service and all related secrets."""
        if not self._is_owner(user):
            raise ServicePermissionError("Недостаточно прав")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            service = connection.execute(
                "SELECT * FROM services WHERE id=?", (int(service_id),)
            ).fetchone()
            if service is None:
                raise ServiceNotFoundError("Сервис не найден")
            if not service["archived_at"]:
                raise ServiceVaultError("Сначала переместите сервис в архив")
            connection.execute("DELETE FROM services WHERE id=?", (int(service_id),))
            connection.commit()
            return dict(service)

    def set_favorite(self, service_id, favorite, user):
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.require(connection, service_id, user, "can_view")
            updated = connection.execute(
                "UPDATE service_user_preferences SET favorite=?,version=version+1,updated_at=? "
                "WHERE service_id=? AND user_id=?",
                (int(bool(favorite)), now, int(service_id), int(user["id"])),
            ).rowcount
            if not updated:
                connection.execute(
                    "INSERT INTO service_user_preferences(service_id,user_id,favorite,sort_order,version,updated_at) "
                    "VALUES(?,?,?,?,1,?)", (service_id, user["id"], int(bool(favorite)), service_id, now)
                )
            connection.commit()

    def reorder(self, ordered_ids, user):
        ids = [int(value) for value in ordered_ids]
        if len(ids) != len(set(ids)):
            raise ServiceVaultError("Некорректный порядок")
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for position, service_id in enumerate(ids):
                self.require(connection, service_id, user, "can_view")
                updated = connection.execute(
                    "UPDATE service_user_preferences SET sort_order=?,version=version+1,updated_at=? "
                    "WHERE service_id=? AND user_id=?", (position, now, service_id, user["id"])
                ).rowcount
                if not updated:
                    connection.execute(
                        "INSERT INTO service_user_preferences(service_id,user_id,favorite,sort_order,version,updated_at) "
                        "VALUES(?,?,0,?,1,?)", (service_id, user["id"], position, now)
                    )
            connection.commit()

    def icon(self, service_id, user):
        with self.connect() as connection:
            service, _rights = self.require(connection, service_id, user, "can_view", allow_archived=True)
            if not service["icon_blob"]:
                raise ServiceNotFoundError("Иконка не найдена")
            return bytes(service["icon_blob"]), service["icon_mime"]
