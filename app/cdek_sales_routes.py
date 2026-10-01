"""Read-only delivery reconciliation plus local manager annotations."""
from urllib.parse import urlsplit

from flask import abort, redirect, render_template, request, url_for

from app.clients.cdek import CdekError
from app.services.cdek_sales import CATEGORIES, WORK, group_sales, order_number_key


def sales_return(value):
    value = str(value or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/sales?source=tictactoy"
    if parsed.scheme or parsed.netloc or parsed.path not in {"/sales", "/app/sales"} or "\\" in value or any(ord(c) < 32 for c in value):
        return "/sales?source=tictactoy"
    return value


def register_cdek_sales_routes(app, service, load_sales, allowed, csrf, actor):
    def authorize():
        if not allowed():
            abort(403)

    def selection(key):
        item = next((r for r in group_sales(load_sales()) if r["id"] == key), None)
        if not item:
            abort(404)
        return item

    @app.get("/sales/cdek")
    def cdek_sales_page():
        authorize()
        args = request.args
        raw_rows = service.rows(load_sales())
        all_rows, counts = service.summary(raw_rows)
        selected = next((r for r in raw_rows if r["id"] == args.get("shipment")), None)
        if selected and selected not in all_rows:
            all_rows.append(selected)
        mode = "all" if args.get("mode") == "all" else "problems"
        category = args.get("category", "") if args.get("category", "") in CATEGORIES else ""
        work = args.get("work", "") if args.get("work", "") in WORK else ""
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
        params = dict(mode=mode, category=category, work=work, urgent="1" if urgent else "", q=query, per_page=size, back=back, sort=sort)
        def link(**changes):
            values = dict(params, page=1)
            values.update(changes)
            return url_for("cdek_sales_page", **{k: v for k, v in values.items() if v != ""})
        return render_template("cdek_sales.html", rows=rows[(page-1)*size:page*size], counts=counts,
            categories=CATEGORIES, category_counts={k: sum(any(i["category"] == k for i in r["issues"]) for r in all_rows) for k in CATEGORIES},
            work_labels=WORK, mode=mode, category=category, work=work, urgent=urgent, query=query,
            total=total, page=page, pages=pages, size=size, back=back, link=link, sort=sort,
            selected=args.get("shipment", ""), message=args.get("message", ""),
            configured=service.delivery.client.configured,
            pvz_warning=service.pvz_warning, pvz_urgent=service.pvz_urgent, transit_days=service.transit_days)

    def finish(key, message):
        # Only preserve recognized list controls; never accept arbitrary redirects.
        params = {k: request.form.get(k, "") for k in ("mode", "category", "work", "urgent", "q", "per_page", "page", "sort")}
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
        except CdekError as error:
            # Preserve the submitted text on conflicts instead of discarding it.
            return render_template("cdek_review_error.html", message=str(error), note=request.form.get("note", ""),
                back=url_for("cdek_sales_page", shipment=key, back=sales_return(request.form.get("back")))), 409 if error.code == "CDEK_CONFLICT" else 400
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
