<?php

define('NO_KEEP_STATISTIC', true);
define('NOT_CHECK_PERMISSIONS', true);
require($_SERVER["DOCUMENT_ROOT"] . "/bitrix/modules/main/include/prolog_before.php");

header('Content-Type: application/json; charset=utf-8');

if (!CModule::IncludeModule("sale")) {
    echo json_encode([
        "status" => "error",
        "message" => "Sale module not found"
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

$orderId = intval($_GET["id"] ?? 0);

if ($orderId <= 0) {
    echo json_encode([
        "status" => "error",
        "message" => "Order id is required"
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

$order = CSaleOrder::GetByID($orderId);

if (!$order) {
    echo json_encode([
        "status" => "error",
        "message" => "Order not found"
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

// The legacy order adapter formats money using currency display settings.
// Read DECIMAL values directly so RUB DECIMALS=0 cannot discard kopecks.
$money = \Bitrix\Main\Application::getConnection()->query(
    "SELECT PRICE, PRICE_DELIVERY, DISCOUNT_VALUE, TAX_VALUE, CURRENCY " .
    "FROM b_sale_order WHERE ID = " . $orderId
)->fetch();
if (!$money) {
    http_response_code(503);
    echo json_encode(["status" => "error", "message" => "Order amounts unavailable"]);
    exit;
}

$products = [];

$dbBasket = CSaleBasket::GetList(
    [],
    ["ORDER_ID" => $orderId],
    false,
    false,
    ["ID", "PRODUCT_ID", "NAME", "QUANTITY", "PRICE", "CURRENCY"]
);

while ($item = $dbBasket->Fetch()) {
    $products[] = [
        "id" => $item["PRODUCT_ID"],
        "name" => $item["NAME"],
        "quantity" => $item["QUANTITY"],
        "price" => $item["PRICE"],
        "currency" => $item["CURRENCY"]
    ];
}

$properties = [];

$dbProps = CSaleOrderPropsValue::GetOrderProps($orderId);

while ($prop = $dbProps->Fetch()) {
    $properties[] = [
        "code" => $prop["CODE"],
        "name" => $prop["NAME"],
        "value" => $prop["VALUE"]
    ];
}

$user = null;

if (!empty($order["USER_ID"])) {
    $rsUser = CUser::GetByID($order["USER_ID"]);
    if ($userData = $rsUser->Fetch()) {
        $user = [
            "id" => $userData["ID"],
            "name" => trim($userData["NAME"] . " " . $userData["LAST_NAME"]),
            "email" => $userData["EMAIL"],
            "phone" => $userData["PERSONAL_PHONE"]
        ];
    }
}

$result = [
    "status" => "ok",
    "order" => [
        "id" => $order["ID"],
        "number" => $order["ACCOUNT_NUMBER"],
        "date" => $order["DATE_INSERT"],
        "status" => $order["STATUS_ID"],
        "price" => $money["PRICE"],
        "delivery_price" => $money["PRICE_DELIVERY"],
        "discount" => $money["DISCOUNT_VALUE"],
        "tax_value" => $money["TAX_VALUE"],
        "currency" => $money["CURRENCY"],
        "paid" => $order["PAYED"],
        "user_id" => $order["USER_ID"],
        "user" => $user,
        "properties" => $properties,
        "products" => $products
    ]
];

echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
