def bitrix_site_status(external_product_id, active):
    element_id = str(external_product_id or "").strip()
    if not element_id:
        return {"key": "unlinked", "label": "Нет связи"}
    if active in (1, True, "1"):
        return {"key": "active", "label": "Активен"}
    if active in (0, False, "0"):
        return {"key": "inactive", "label": "Выключен"}
    return {"key": "unknown", "label": "Неизвестно"}
