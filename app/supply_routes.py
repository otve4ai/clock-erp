"""Receipt workspace: no MoySklad dependency; legacy writes are retired."""
import sqlite3
from functools import wraps
from flask import request, jsonify, render_template, abort, redirect
from app.auth import current_auth_user, auth_is_enabled, require_csrf_when_authenticated
from app.services.supplies import SupplyEngine, SupplyError
from app.services.receipt_inventory import ReceiptInventory, ReceiptInventoryError
from app.services.manual_receipts import ManualReceipts, ManualReceiptError, REASONS
from app.services.excel_receipt_import import ExcelDraftError, MAX_EXCEL_FILE_SIZE
from app.clients.bitrix_catalog import BitrixCatalogReadOnlyError


def register_supply_routes(w):
    app = w.app
    original_collection = app.view_functions["api_receipts_collection"]
    original_resource = app.view_functions["api_receipt_resource"]

    def actor():
        user = current_auth_user() or {}
        return ' '.join(filter(None, [user.get('first_name'), user.get('last_name')])) or user.get('email') or 'system'

    def writable():
        require_csrf_when_authenticated()
        user = current_auth_user() or {}
        if auth_is_enabled() and user.get('role') in ('viewer', 'readonly', 'read_only'):
            abort(403)

    def admin_only():
        user = current_auth_user() or {}
        if auth_is_enabled() and user.get('role') != 'admin':
            abort(403)

    def guarded(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                if request.method != 'GET':
                    writable()
                return fn(*args, **kwargs)
            except (SupplyError, ReceiptInventoryError, ManualReceiptError, ExcelDraftError, ValueError) as error:
                preview = getattr(error, 'preview', None)
                return jsonify(ok=False, message=str(error), data=preview), 409 if preview else 422
            except BitrixCatalogReadOnlyError:
                return jsonify(ok=False, message='Bitrix недоступен. Сохранённые поставки можно проводить без Bitrix.'), 503
            except sqlite3.Error:
                app.logger.exception('Local supply database operation failed')
                return jsonify(ok=False, message='Не удалось сохранить поставку. Изменение остатка не выполнено.'), 503
        return wrapped

    @guarded
    def page():
        return render_template('supplies.html')

    @app.route('/api/v1/receipts/supplies', methods=['GET', 'POST'])
    @guarded
    def supplies():
        engine = SupplyEngine()
        if request.method == 'POST':
            p = request.get_json(silent=True) or {}
            if not isinstance(p, dict):
                raise SupplyError('Некорректные данные поставки.')
            return jsonify(ok=True, data=engine.create(p.get('title'), p.get('comment'), actor(), items=p.get('items'), key=request.headers.get('Idempotency-Key'), warehouse_id=p.get('warehouse_id'))), 201
        return jsonify(ok=True, data=engine.list())

    @app.route('/api/v1/receipts/warehouses', methods=['GET'])
    @guarded
    def warehouses():
        return jsonify(ok=True, data=ManualReceipts().warehouses())

    @app.route('/api/v1/receipts/manual', methods=['GET', 'POST'])
    @guarded
    def manual_receipts():
        engine = ManualReceipts()
        if request.method == 'GET':
            return jsonify(ok=True, data=engine.list(), reasons=REASONS)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise ManualReceiptError('Некорректные данные прихода.')
        result = engine.create(
            payload.get('warehouse_id'), payload.get('reason_code'),
            payload.get('items'), payload.get('comment'), actor(),
            request.headers.get('Idempotency-Key'),
        )
        return jsonify(ok=True, data=result), 201

    @app.route('/api/v1/receipts/manual/<receipt_id>', methods=['GET', 'PATCH', 'DELETE'])
    @guarded
    def manual_receipt(receipt_id):
        engine = ManualReceipts()
        if request.method == 'GET':
            return jsonify(ok=True, data=engine.get(receipt_id))
        if request.method == 'DELETE':
            return jsonify(ok=True, data=engine.delete(receipt_id, actor()))
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise ManualReceiptError('Некорректные данные прихода.')
        return jsonify(ok=True, data=engine.update(
            receipt_id, payload.get('warehouse_id'), payload.get('reason_code'),
            payload.get('items'), payload.get('comment'), actor(),
        ))

    @app.route('/api/v1/receipts/manual/<receipt_id>/post', methods=['POST'])
    @guarded
    def post_manual_receipt(receipt_id):
        return jsonify(ok=True, data=ManualReceipts().post(receipt_id, actor()))

    @app.route('/api/v1/receipts/manual/<receipt_id>/cancel', methods=['POST'])
    @guarded
    def cancel_manual_receipt(receipt_id):
        return jsonify(ok=True, data=ManualReceipts().cancel(receipt_id, actor()))

    @app.route('/api/v1/receipts/documents', methods=['GET'])
    @guarded
    def incoming_documents():
        supplies_rows = [dict(row, source_type='supply') for row in SupplyEngine().list()]
        manual_rows = [dict(row, source_type='manual_receipt') for row in ManualReceipts().list()]
        cancellations = []
        for row in ReceiptInventory().list_sale_cancellation_receipts():
            cancellations.append({
                'id': row.get('id'), 'source_id': row.get('source_sale_id'),
                'source_type': 'sale_cancellation', 'number': row.get('number'),
                'title': row.get('number'), 'comment': row.get('note') or '',
                'created_at': row.get('created_at') or row.get('receipt_date') or '',
                'created_by': row.get('user_name') or '', 'warehouse_name': 'Основной склад',
                'position_count': row.get('positions_count') or len(row.get('positions') or []),
                'total_quantity': row.get('total_quantity') or sum(float(item.get('quantity') or 0) for item in row.get('positions') or []),
                'status': row.get('status') or 'posted', 'items': row.get('positions') or [],
            })
        data = sorted(supplies_rows + manual_rows + cancellations,
                      key=lambda row: (row.get('created_at') or '', str(row.get('id') or '')), reverse=True)
        return jsonify(ok=True, data=data)

    @app.route('/api/v1/receipts/supplies/<supply_id>', methods=['GET', 'PATCH', 'DELETE'])
    @guarded
    def supply(supply_id):
        engine = SupplyEngine()
        if request.method == 'DELETE':
            admin_only()
            return jsonify(ok=True, data=engine.delete(supply_id, actor()))
        if request.method == 'PATCH':
            p = request.get_json(silent=True) or {}
            if not isinstance(p, dict):
                raise SupplyError('Некорректные данные поставки.')
            return jsonify(ok=True, data=engine.update(supply_id, p.get('title'), p.get('comment'), p.get('items')))
        return jsonify(ok=True, data=engine.get(supply_id))

    @app.route('/api/v1/receipts/supplies/<supply_id>/delete-preview', methods=['GET'])
    @guarded
    def supply_delete_preview(supply_id):
        admin_only()
        return jsonify(ok=True, data=SupplyEngine().preview_delete(supply_id))

    @app.route('/api/v1/receipts/supplies/<supply_id>/details', methods=['PATCH'])
    @guarded
    def supply_details(supply_id):
        admin_only()
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise SupplyError('Некорректные данные поставки.')
        return jsonify(ok=True, data=SupplyEngine().update_details(
            supply_id, payload.get('title'), payload.get('comment'), actor(),
        ))

    @app.route('/api/v1/receipts/supplies/<supply_id>/items', methods=['POST'])
    @guarded
    def add_item(supply_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise SupplyError('Некорректные данные товара.')
        return jsonify(ok=True, data=SupplyEngine().add_item(
            supply_id, payload.get('product_id'), payload.get('quantity'),
            request.headers.get('Idempotency-Key'), actor(),
        ))

    @app.route('/api/v1/receipts/supplies/<supply_id>/post', methods=['POST'])
    @guarded
    def post(supply_id):
        return jsonify(ok=True, data=SupplyEngine().post(supply_id, actor()))

    @app.route('/api/v1/receipts/bitrix', methods=['GET'])
    @guarded
    def search():
        query = (request.args.get('q') or '').strip()
        if len(query) < 2:
            return jsonify(ok=True, data=[])
        products = w._bitrix_single_client().search_products(query, limit=20)
        rows = [w._bitrix_single_source_payload(p) for p in products]
        for row in rows:
            row.pop('stock', None)
        return jsonify(ok=True, data=rows)

    @app.route('/api/v1/receipts/bitrix/<int:bitrix_id>', methods=['POST'])
    @guarded
    def resolve(bitrix_id):
        product = w._bitrix_single_client().get_product(bitrix_id)
        if not product:
            raise SupplyError('Товар не найден в Bitrix.')
        return jsonify(ok=True, data=SupplyEngine().resolve_bitrix(product))

    @app.route('/api/v1/receipts/excel', methods=['POST'])
    @guarded
    def excel():
        upload = request.files.get('file')
        if not upload:
            raise SupplyError('Выберите Excel-файл.')
        content = upload.read(MAX_EXCEL_FILE_SIZE + 1)
        if len(content) > MAX_EXCEL_FILE_SIZE:
            raise SupplyError('Файл превышает 15 МБ.')
        return jsonify(ok=True, data=SupplyEngine().import_excel(content, upload.filename, actor())), 201

    @app.route('/api/v1/receipts/movements', methods=['GET'])
    @guarded
    def movements():
        return jsonify(ok=True, data=SupplyEngine().movements(w.load_receipts()))

    @guarded
    def retired(*args, **kwargs):
        return jsonify(ok=False, message='Старый способ изменения прихода отключён. Используйте поставку ERP: /app/receipts.'), 410

    @guarded
    def legacy_collection():
        if request.method != 'GET':
            return retired()
        return original_collection()

    @guarded
    def legacy_resource(receipt_id):
        if request.method != 'GET':
            return retired()
        return original_resource(receipt_id)

    app.view_functions['receipts_page'] = page
    # Retire ALL old mutation entry points, including both Excel implementations.
    for name in ('receipt_catalog_create', 'receipt_create', 'receipt_update', 'receipt_delete',
                 'receipts_import_preview', 'excel_receipt_post'):
        app.view_functions[name] = retired
    app.view_functions['api_receipts_collection'] = legacy_collection
    app.view_functions['api_receipt_resource'] = legacy_resource
    app.view_functions['excel_receipt_preview'] = excel
    app.view_functions['excel_receipt_new'] = lambda: redirect('/app/receipts?tab=supplies')
