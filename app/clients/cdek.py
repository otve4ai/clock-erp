"""CDEK v2 order reads. Credentials and response bodies are never logged."""

import math
import os
import threading
import time

import requests


class CdekError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class CdekClient:
    origin = "https://api.cdek.ru/v2"

    def __init__(self, account=None, password=None, session=None, clock=None):
        self._account = str(account if account is not None else os.getenv("CDEK_ACCOUNT", "")).strip()
        self._password = str(password if password is not None else os.getenv("CDEK_PASSWORD", "")).strip()
        self._session = session or requests.Session()
        self._clock = clock or time.monotonic
        self._token = ""
        self._expires = 0
        self._lock = threading.RLock()

    @property
    def configured(self):
        return bool(self._account and self._password)

    def _request(self, method, path, **kwargs):
        try:
            response = self._session.request(
                method, self.origin + path, timeout=(5, 20),
                allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            raise CdekError("CDEK_NETWORK", "СДЭК временно недоступен. Повторите позже.") from None
        codes = {
            401: ("CDEK_UNAUTHORIZED", "СДЭК отклонил ключ API."),
            403: ("CDEK_FORBIDDEN", "Нет доступа к отправлению по этому договору СДЭК."),
            404: ("CDEK_NOT_FOUND", "Отправление СДЭК не найдено."),
            429: ("CDEK_RATE_LIMIT", "Лимит запросов СДЭК. Повторите позже."),
        }
        if response.status_code in codes:
            raise CdekError(*codes[response.status_code])
        if not 200 <= response.status_code < 300:
            raise CdekError("CDEK_HTTP", "СДЭК не смог выполнить запрос (HTTP {}).".format(response.status_code))
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise CdekError("CDEK_RESPONSE", "Некорректный ответ СДЭК.") from None
        if not isinstance(result, dict):
            raise CdekError("CDEK_RESPONSE", "Некорректный ответ СДЭК.")
        return result

    def _authorize(self):
        if not self.configured:
            raise CdekError("CDEK_NOT_CONFIGURED", "Ключ API СДЭК ещё не подключён к ERP.")
        if self._token and self._clock() < self._expires:
            return
        result = self._request("POST", "/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": self._account, "client_secret": self._password,
        }, headers={"Accept": "application/json"})
        token = result.get("access_token")
        try:
            lifetime = float(result.get("expires_in", 0))
        except (ValueError, TypeError):
            lifetime = 0
        if not isinstance(token, str) or not token or not math.isfinite(lifetime) or lifetime <= 0:
            raise CdekError("CDEK_UNAUTHORIZED", "СДЭК не выдал токен доступа.")
        self._token = token
        self._expires = self._clock() + max(0, lifetime - 60)

    def get_registries(self, day):
        """Payment registries for one payment date, using the existing credentials."""
        from datetime import datetime
        datetime.strptime(day, "%Y-%m-%d")
        with self._lock:
            for attempt in range(2):
                self._authorize()
                try:
                    result = self._request("GET", "/registries", params={"date": day}, headers={
                        "Accept": "application/json", "Authorization": "Bearer " + self._token,
                    })
                    break
                except CdekError as error:
                    if error.code != "CDEK_UNAUTHORIZED" or attempt:
                        raise
                    self._token = ""
                    self._expires = 0
        # CDEK returns an empty object on dates without registries.
        if result == {}:
            return []
        if not isinstance(result.get("registries"), list):
            raise CdekError("CDEK_RESPONSE", "Не удалось прочитать реестры перечислений.")
        return result["registries"]

    def get_order(self, cdek_number="", im_number=""):
        """Fetch one shipment; explicit waybill takes precedence over shop number."""
        value = str(cdek_number or im_number or "").strip()
        if not value or len(value) > 255:
            raise CdekError("CDEK_REFERENCE", "Не указан номер заказа или накладной СДЭК.")
        params = {"cdek_number" if cdek_number else "im_number": value}
        with self._lock:
            for attempt in range(2):
                self._authorize()
                try:
                    result = self._request("GET", "/orders", params=params, headers={
                        "Accept": "application/json", "Authorization": "Bearer " + self._token,
                    })
                    break
                except CdekError as error:
                    if error.code != "CDEK_UNAUTHORIZED" or attempt:
                        raise
                    self._token = ""
                    self._expires = 0
        entity = result.get("entity")
        if not isinstance(entity, dict) or not entity.get("cdek_number"):
            raise CdekError("CDEK_NOT_FOUND", "СДЭК не вернул отправление. Проверьте номер и договор.")
        # Never attach a response for a different order, even if the API returns 200.
        actual = entity.get("cdek_number" if cdek_number else "number")
        if str(actual or "").strip() != value:
            raise CdekError("CDEK_MISMATCH", "Номер отправления в ответе СДЭК не совпадает с запросом.")
        return entity
