from pathlib import Path
import json
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_DIR = PROJECT_ROOT / "data" / "cleaned"
OUTPUT_DIR = PROJECT_ROOT / "data" / "ai_ready"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_cleaned_data():
    return {
        "customer": pd.read_csv(INPUT_DIR / "customer_cleaned.csv"),
        "order_header": pd.read_csv(INPUT_DIR / "order_header_cleaned.csv"),
        "order_detail": pd.read_csv(INPUT_DIR / "order_detail_cleaned.csv"),
        "product": pd.read_csv(INPUT_DIR / "product_cleaned.csv"),
        "category": pd.read_csv(INPUT_DIR / "category_cleaned.csv"),
    }


def safe_value(value):
    if pd.isna(value):
        return None
    return value


def main():
    data = load_cleaned_data()

    customer = data["customer"]
    order_header = data["order_header"]
    order_detail = data["order_detail"]
    product = data["product"]
    category = data["category"]

    # Merge order header with customer
    order_customer = order_header.merge(
        customer,
        on="customerid",
        how="left",
        suffixes=("_order", "_customer")
    )

    # Merge order details with product
    detail_product = order_detail.merge(
        product,
        on="productid",
        how="left",
        suffixes=("_detail", "_product")
    )

    # Merge product with category
    detail_product_category = detail_product.merge(
        category,
        on="productcategoryid",
        how="left",
        suffixes=("", "_category")
    )

    ai_records = []

    for _, order in order_customer.iterrows():
        salesorderid = order["salesorderid"]

        items = detail_product_category[
            detail_product_category["salesorderid"] == salesorderid
        ]

        product_list = []
        product_names = []

        for _, item in items.iterrows():
            product_name = safe_value(item.get("name"))
            category_name = safe_value(item.get("name_category"))

            product_names.append(product_name)

            product_list.append({
                "product_id": safe_value(item.get("productid")),
                "product_name": product_name,
                "category": category_name,
                "quantity": safe_value(item.get("orderqty")),
                "unit_price": safe_value(item.get("unitprice")),
                "line_total": safe_value(item.get("linetotal")),
            })

        customer_name = " ".join(
            str(x) for x in [
                safe_value(order.get("firstname")),
                safe_value(order.get("middlename")),
                safe_value(order.get("lastname"))
            ] if x is not None
        )

        total_due = safe_value(order.get("totaldue"))
        order_date = safe_value(order.get("orderdate"))
        sales_order_number = safe_value(order.get("salesordernumber"))

        ai_text = (
            f"Sales order {sales_order_number} was placed by customer {customer_name}. "
            f"The order total amount is {total_due}. "
            f"The order contains products: {', '.join([str(p) for p in product_names if p])}. "
            f"This record represents an ERP sales transaction with customer, order, product, and category context."
        )

        record = {
            "record_id": f"sales_order_{salesorderid}",
            "record_type": "sales_order",
            "source_tables": [
                "customer",
                "order_header",
                "order_detail",
                "product",
                "category"
            ],
            "sales_order_id": safe_value(salesorderid),
            "sales_order_number": sales_order_number,
            "order_date": order_date,
            "customer": {
                "customer_id": safe_value(order.get("customerid")),
                "name": customer_name,
                "company": safe_value(order.get("companyname")),
                "email": safe_value(order.get("emailaddress")),
                "phone": safe_value(order.get("phone")),
            },
            "financial_summary": {
                "subtotal": safe_value(order.get("subtotal")),
                "tax": safe_value(order.get("taxamt")),
                "freight": safe_value(order.get("freight")),
                "total_due": total_due,
            },
            "items": product_list,
            "text_for_ai": ai_text,
            "metadata": {
                "business_domain": "sales",
                "data_source": "AdventureWorksLT2019",
                "transformation_type": "erp_aware_record_linking",
            }
        }

        ai_records.append(record)

    output_json = OUTPUT_DIR / "erp_sales_records.json"
    output_jsonl = OUTPUT_DIR / "erp_sales_records.jsonl"

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(ai_records, f, indent=2, ensure_ascii=False, default=str)

    with open(output_jsonl, "w", encoding="utf-8") as f:
        for record in ai_records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    print(f"Created {len(ai_records)} AI-ready ERP records")
    print(f"Saved JSON: {output_json}")
    print(f"Saved JSONL: {output_jsonl}")


if __name__ == "__main__":
    main()