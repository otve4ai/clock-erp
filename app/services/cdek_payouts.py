"""Independent payment reconciliation. No writes to orders, stock or CDEK."""
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.clients.cdek import CdekClient, CdekError
from app.services.cdek_delivery import MOSCOW

LABELS = {"pending": "Ждём перечисления", "expected": "Ждём оплаты покупателя",
          "paid": "Перечислено", "other": "Без ожидаемой выплаты", "unknown": "Не проверено"}
TRACK = re.compile(r"[0-9]{8,20}\Z")


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise CdekError("CDEK_RESPONSE", "Некорректная сумма в данных СДЭК.") from None


def iso_day(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        raise CdekError("CDEK_RESPONSE", "Некорректная дата в данных СДЭК.") from None


def money(value):
    return "{:,.2f}".format(amount(value)).replace(",", " ").replace(".", ",") + " ₽"


def compact_order(entity):
    """Keep financial evidence, never recipient contacts or addresses."""
    statuses = [s for s in entity.get("statuses", []) if not s.get("deleted")]
    if not statuses or not TRACK.fullmatch(str(entity.get("cdek_number", ""))):
        raise CdekError("CDEK_RESPONSE", "Нет номера или статуса накладной.")
    current = max(statuses, key=lambda s: s.get("date_time", ""))
    detail = entity.get("delivery_detail") or {}
    cod = Decimal(0)
    for package in entity.get("packages", []):
        for item in package.get("items", []):
            cod += amount((item.get("payment") or {}).get("value", 0)) * amount(item.get("amount", 0))
    cod += amount((entity.get("delivery_recipient_cost") or {}).get("value", 0))
    returned = bool(entity.get("is_return") or entity.get("is_reverse") or entity.get("is_client_return"))
    return dict(track=str(entity["cdek_number"]), order=str(entity.get("number") or ""),
                created=min(iso_day(s.get("date_time")) for s in statuses),
                delivered=iso_day(detail["date"]) if detail.get("date") else "",
                status=str(current.get("code") or ""), delivery=str(current.get("name") or ""),
                returned=returned, currency=entity.get("recipient_currency") or "RUB",
                cod=str(Decimal(0) if returned else cod),
                collected=str(amount(detail["payment_sum"])) if detail.get("payment_sum") is not None else None,
                cost=str(amount(detail["total_sum"])) if detail.get("total_sum") is not None else None)


def clean_registries(registries, day):
    clean = []
    for reg in registries:
        if not isinstance(reg, dict) or not reg.get("registry_number") or not isinstance(reg.get("orders"), list):
            raise CdekError("CDEK_RESPONSE", "Неполный реестр перечисления.")
        paid_date = iso_day(reg.get("payment_date"))
        if paid_date != day:
            raise CdekError("CDEK_RESPONSE", "Дата оплаты реестра не совпадает с запросом.")
        entries = []
        for row in reg["orders"]:
            track = str(row.get("cdek_number") or "")
            if not TRACK.fullmatch(track):
                raise CdekError("CDEK_RESPONSE", "В реестре отсутствует номер накладной.")
            entries.append(dict(track=track, gross=str(amount(row.get("payment_sum"))),
                                net=str(amount(row.get("transfer_sum"))),
                                basis=str(row.get("basis_type") or "")))
        clean.append(dict(number=str(reg["registry_number"]), date=paid_date,
                          total=str(amount(reg.get("sum"))), entries=entries))
    return clean


def reconcile(orders, registries, since):
    ledger = {}
    unique = {}
    for reg in registries:
        key = (reg["number"], reg["date"])
        if key in unique:
            if reg != unique[key]:
                raise CdekError("CDEK_RESPONSE", "Противоречивые данные одного реестра.")
            continue
        unique[key] = reg
        for entry in reg["entries"]:
            ledger.setdefault(entry["track"], []).append(dict(entry, date=reg["date"], registry=reg["number"]))
    rows = []
    for order in orders:
        row = dict(order)
        entries = ledger.get(row["track"], [])
        gross = sum((amount(e["gross"]) for e in entries), Decimal(0))
        net = sum((amount(e["net"]) for e in entries), Decimal(0))
        collected = amount(row["collected"] or 0)
        remaining = max(Decimal(0), collected - gross)
        status, reason = "other", "Без наложенного платежа"
        if row.get("lookup_failed"):
            status, reason = "unknown", "Накладная не найдена при последней проверке"
        elif row["currency"] != "RUB":
            status, reason = "unknown", "Валюта отличается от RUB"
        elif row["returned"]:
            reason = "Возвратная накладная"
        elif gross > collected or gross < 0:
            status, reason = "unknown", "Суммы оплаты и реестра требуют сверки"
        elif collected > 0 and gross == collected:
            status, reason = "paid", "Перечисление отражено в реестре"
        elif collected > 0:
            if not row["delivered"] or row["delivered"] < since:
                status, reason = "unknown", "Реестры не покрывают дату получения оплаты"
            else:
                status, reason = "pending", "Перечисление не найдено" if not gross else "Частичное перечисление"
        elif row["status"] in {"NOT_DELIVERED", "REMOVED", "POSTOMAT_SEIZED"}:
            reason = "Не вручён / возврат / отменён"
        elif amount(row["cod"]) > 0:
            if row["status"] in {"DELIVERED", "POSTOMAT_RECEIVED", "INVALID"}:
                status, reason = "unknown", "Нет подтверждённой суммы оплаты"
            else:
                status, reason = "expected", "Покупатель ещё не оплатил"
        estimate = None
        if status == "pending" and row["cost"] is not None and gross == 0:
            estimate = str(max(Decimal(0), remaining - amount(row["cost"])))
        row.update(category=status, label=LABELS[status], reason=reason,
                   remaining=str(remaining), net=str(net), entries=entries, estimate=estimate,
                   paid_date=max((e["date"] for e in entries), default=""))
        rows.append(row)
    return rows, list(unique.values())


class CdekPayouts:
    def __init__(self, path=None, client=None, clock=None):
        self.path = Path(path or os.getenv("CDEK_PAYOUTS_DIR") or "instance/cdek/payouts")
        self.client = client if client is not None else CdekClient()
        self.clock = clock or time.time

    def today(self):
        return datetime.fromtimestamp(self.clock(), MOSCOW).date()

    def read(self, name, default):
        try:
            value = json.loads((self.path / name).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except FileNotFoundError:
            return default
        except (ValueError, OSError):
            raise CdekError("CDEK_CACHE", "Не удалось прочитать данные выплат СДЭК.") from None

    def write(self, name, value):
        self.path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".payouts-", dir=str(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(self.path / name))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def snapshot(self):
        value = self.read("snapshot.json", {})
        if value and (value.get("version") != 1 or not isinstance(value.get("rows"), list)
                      or not isinstance(value.get("registries"), list)):
            raise CdekError("CDEK_CACHE", "Повреждён снимок выплат СДЭК.")
        return value

    def sync_pending(self, references, manual=False, budget=150, limit=100):
        """Resume bounded work; publish atomically only after the entire cohort is read."""
        started = self.clock()
        old = self.snapshot()
        imported = self.read("sources.json", {"tracks": [], "since": ""})
        stage = self.read("stage.json", {})
        if not stage:
            since = min(v for v in [(self.today()-timedelta(days=44)).isoformat(),
                                    old.get("since"), imported.get("since")] if v)
            if (self.today()-datetime.strptime(since, "%Y-%m-%d").date()).days > 730:
                raise CdekError("CDEK_RANGE", "История превышает два года. Нужна отдельная архивная сверка.")
            refs = set(str(t) for t in imported.get("tracks", []))
            refs.update(r["track"] for r in old.get("rows", []))
            refs.update(str(r.get("cdek_number") or "") for r in references)
            refs = {t for t in refs if TRACK.fullmatch(t)}
            ims = sorted({str(r["im_number"]) for r in references if r.get("im_number") and not r.get("cdek_number")})
            stage = dict(since=since, until=self.today().isoformat(), day=since, registries=[],
                         tracks=sorted(refs), ims=ims, queue=None, cursor=0, orders=[], unresolved=[])
        calls = 0
        while stage["day"] <= stage["until"] and calls < limit and self.clock()-started < budget:
            regs = clean_registries(self.client.get_registries(stage["day"]), stage["day"])
            stage["registries"].extend(regs)
            stage["day"] = (datetime.strptime(stage["day"], "%Y-%m-%d").date()+timedelta(days=1)).isoformat()
            calls += 1
            self.write("stage.json", stage)
        if stage["day"] > stage["until"] and stage["queue"] is None:
            tracks = set(stage["tracks"])
            tracks.update(e["track"] for r in stage["registries"] for e in r["entries"])
            stage["queue"] = [{"cdek_number": t} for t in sorted(tracks)] + [{"im_number": n} for n in stage["ims"]]
        queue = stage["queue"] or []
        while stage["queue"] is not None and stage["cursor"] < len(queue) and calls < limit and self.clock()-started < budget:
            reference = queue[stage["cursor"]]
            try:
                order = compact_order(self.client.get_order(**reference))
                stage["orders"].append(order)
            except CdekError as error:
                if error.code != "CDEK_NOT_FOUND":
                    raise
                previous = next((r for r in old.get("rows", []) if r["track"] == reference.get("cdek_number")), None)
                if previous:
                    stage["orders"].append(dict(previous, lookup_failed=True))
                stage.setdefault("unresolved", []).append(reference)
            stage["cursor"] += 1
            calls += 1
            self.write("stage.json", stage)
        remaining = len(queue)-stage["cursor"] if stage["queue"] is not None else 1
        if not remaining:
            dedup = {r["track"]: r for r in stage["orders"]}
            rows, regs = reconcile(list(dedup.values()), stage["registries"], stage["since"])
            self.write("snapshot.json", dict(version=1, rows=rows, registries=regs,
                       since=stage["since"], until=stage["until"], checked_at=self.clock(),
                       imported_count=len(imported.get("tracks", [])), unresolved=stage.get("unresolved", [])))
            self.write("stage.json", {})
        return dict(updated=calls, errors=0, skipped=0, remaining=remaining)


def discover_references(delivery, sales):
    from app.services.cdek_sales import group_sales
    result = []
    for row in group_sales(sales):
        if TRACK.fullmatch(row["tracking"]):
            result.append({"cdek_number": row["tracking"]})
        elif not row["tracking"] and row["number"]:
            result.append({"im_number": row["number"]})
    for path in delivery.path.glob("*.json"):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            t = str(cached.get("reference", {}).get("cdek_number") or "")
            if TRACK.fullmatch(t):
                result.append({"cdek_number": t})
        except (ValueError, OSError, AttributeError):
            raise CdekError("CDEK_CACHE", "Не удалось прочитать список накладных ERP.") from None
    return result
