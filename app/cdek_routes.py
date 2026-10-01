"""CDEK status refresh with existing ERP access and CSRF checks."""
from flask import abort, redirect, url_for

from app.clients.cdek import CdekError


def register_cdek_routes(app, service, can_view, require_csrf, load_order, tracking):
    @app.post("/order/<int:order_id>/cdek/sync")
    def cdek_delivery_sync(order_id):
        if not can_view():
            abort(403)
        require_csrf()
        order = load_order(order_id)
        if not order:
            abort(404)
        try:
            service.sync(order, tracking=tracking(order))
            notice, message = "success", "Статус доставки СДЭК обновлён"
        except CdekError as error:
            notice, message = "error", str(error)
        return redirect(url_for("order_page", order_id=order_id, notice=notice, message=message))
