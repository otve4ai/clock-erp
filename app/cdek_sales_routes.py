"""Read-only delivery reconciliation plus local manager annotations."""
from urllib.parse import urlsplit

from flask import jsonify, abort, redirect, render_template, request, url_for

from app.clients.cdek import CdekError
from app.services.cdek_sync import CdekSync
from app.services.cdek_sales import CATEGORIES, WORK, OUTCOMES, group_sales, order_number_key
from app.services.cdek_delivery import display_time


def sales_return(value):
    value = str(value or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/sales?source=tictactoy"
    if parsed.scheme or parsed.netloc or parsed.path not in {"/sales", "/app/sales"} or "\\" in value or any(ord(c) < 32 for c in value):
        return "/sales?source=tictactoy"
    return value


def register_cdek_sales_routes(app, service, load_sales, allowed, csrf, actor, find_orders=None):
    def authorize():
        if not allowed():
            abort(403)

    sync = CdekSync(service.delivery)

    @app.get("/sales/cdek/contacts")
    def cdek_sales_contacts():
        authorize()
        keys = request.args.getlist("id")
        if len(keys) > 51:
            abort(400)
        rows = service.rows(load_sales())
        response = jsonify({row["id"]: row["contacts"] for row in rows if row["id"] in keys})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/sales/cdek/sync-status", methods=["GET", "POST"])
    def cdek_sales_sync_status():
        authorize()
        try:
            if request.method == "POST":
                csrf()
                sync.start(lambda: group_sales(load_sales()), app)
            result = sync.summary()
            _, counts = service.summary(service.rows(load_sales()))
            return jsonify(dict(result, counts=counts)), 202 if request.method == "POST" else 200
        except CdekError as error:
            return jsonify(message=str(error)), 409 if error.code == "CDEK_BUSY" else 503

    def selection(key):
        item = next((r for r in group_sales(load_sales()) if r["id"] == key), None)
        if not item:
            abort(404)
        return item

    @app.get("/sales/cdek/order/<number>")
    def cdek_sales_order(number):
        authorize()
        candidates = find_orders(number) if find_orders else []
        matches = [order for order in candidates
                   if str(order.get("number") or order.get("ACCOUNT_NUMBER") or "").strip() == number]
        if not matches:
            matches = [order for order in candidates
                       if str(order.get("id") or order.get("ID") or "") == number]
        if len(matches) == 1:
            order_id = str(matches[0].get("id") or matches[0].get("ID") or "")
            if order_id and all("0" <= char <= "9" for char in order_id):
                return redirect(url_for("order_page", order_id=int(order_id)))
        return redirect(url_for("orders_page", source="tictactoy", q=number, period="all", status="all"))

    @app.get("/sales/cdek")
    def cdek_sales_page():
        authorize()
        return render_page(request.args)

    def render_page(args, submitted=None, error=None):
        raw_rows = service.rows(load_sales())
        all_rows, counts = service.summary(raw_rows)
        selected = next((r for r in raw_rows if r["id"] == args.get("shipment")), None)
        if selected and selected not in all_rows:
            all_rows.append(selected)
        mode = "all" if args.get("mode") == "all" else "problems"
        category = args.get("category", "") if args.get("category", "") in CATEGORIES else ""
        status = args.get("status", "")
        if status not in {"В пути", "В ПВЗ", "У курьера", "Вручён"}:
            status = ""
        work = args.get("work", "") if args.get("work", "") in WORK else ""
        if work == "closed":
            mode, category = "all", ""
        urgent = args.get("urgent") == "1"
        query = str(args.get("q") or "").strip()[:255]
        sort = args.get("sort", "priority")
        if sort not in {"priority", "order_asc", "order_desc", "date_asc", "date_desc"}:
            sort = "priority"
        try:
            size = 50 if int(args.get("per_page", 25)) == 50 else 25
            page = max(1, int(args.get("page", 1)))
        except (ValueError, TypeError):
            page, size = 1, 25
        rows = [r for r in all_rows if (mode == "all" or r["issues"])
                and (not status or r["label"] == status)
                and (not category or any(i["category"] == category for i in r["issues"]))
                and (not work or r["work"] == work)
                and (not urgent or r["priority"] == 2)
                and (not query or query in " ".join(r["orders"] + [r["tracking"], r["delivery"].get("cdek_number", "")]))]
        if sort in {"order_asc", "order_desc"}:
            rows = sorted((r for r in rows if r["orders"]), key=order_number_key,
                          reverse=sort == "order_desc") + [r for r in rows if not r["orders"]]
        elif sort in {"date_asc", "date_desc"}:
            rows = sorted((r for r in rows if r["order_date"]), key=lambda r: r["order_date"],
                          reverse=sort == "date_desc") + [r for r in rows if not r["order_date"]]
        total = len(rows)
        if selected and not args.get("page"):
            page = next((i // size + 1 for i, r in enumerate(rows) if r["id"] == selected["id"]), page)
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        back = sales_return(args.get("back"))
        params = dict(mode=mode, status=status, category=category, work=work, urgent="1" if urgent else "", q=query, per_page=size, back=back, sort=sort)
        def link(**changes):
            values = dict(params, page=1)
            values.update(changes)
            return url_for("cdek_sales_page", **{k: v for k, v in values.items() if v != ""})
        return render_template("cdek_sales.html", rows=rows[(page-1)*size:page*size], counts=counts,
            categories=CATEGORIES, category_counts={k: sum(any(i["category"] == k for i in r["issues"]) for r in all_rows) for k in CATEGORIES},
            work_labels=WORK, outcomes=OUTCOMES, display_time=display_time, selected_row=selected,
            form_values=submitted, form_error=str(error) if error else "",
            error_field=getattr(error, "field", "") if error else "",
            mode=mode, status=status, category=category, work=work, urgent=urgent, query=query,
            total=total, page=page, pages=pages, size=size, back=back, link=link, sort=sort,
            selected=args.get("shipment", ""), message=args.get("message", ""),
            configured=service.delivery.client.configured,
            pvz_warning=service.pvz_warning, pvz_urgent=service.pvz_urgent, transit_days=service.transit_days)

    def finish(key, message):
        # Only preserve recognized list controls; never accept arbitrary redirects.
        params = {k: request.form.get(k, "") for k in ("mode", "status", "category", "work", "urgent", "q", "per_page", "page", "sort")}
        params.update(back=sales_return(request.form.get("back")), shipment=key, message=message)
        return redirect(url_for("cdek_sales_page", **params))

    @app.post("/sales/cdek/<key>/review")
    def cdek_sales_review(key):
        authorize()
        csrf()
        item = selection(key)
        try:
            payload = dict(request.form)
            payload["work"] = request.form.get("review_work", "new")
            service.save_review(item, payload, actor())
        except (CdekError, OSError) as error:
            if isinstance(error, OSError):
                error = CdekError("CDEK_SAVE", "Не удалось сохранить изменения. Введённые данные сохранены в форме; повторите попытку.")
            args = dict(request.form, shipment=key)
            return render_page(args, submitted=dict(request.form), error=error), 409 if error.code == "CDEK_CONFLICT" else 400
        return finish(key, "Отметка менеджера сохранена")

    @app.post("/sales/cdek/<key>/sync")
    def cdek_sales_sync(key):
        authorize()
        csrf()
        item = selection(key)
        try:
            if not item.get("tracking") and not item.get("number"):
                raise CdekError("CDEK_REFERENCE", "Укажите номер заказа или накладную в продаже.")
            service.delivery.sync(item)
            message = "Статус СДЭК обновлён"
        except CdekError as error:
            message = str(error)
        return finish(key, message)
