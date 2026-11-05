"""
setup_demo.py — Tạo dữ liệu mẫu cho nhiệm vụ demo: sandbox/sales.csv (30 dòng).

Dữ liệu cố định (không ngẫu nhiên) nên đáp án luôn như nhau, dễ kiểm tra.
Có cài sẵn hai "bẫy" nhỏ để xem agent tính có đúng không:
  - Tháng 1 và 2: dòng có doanh thu lớn nhất KHÔNG thuộc sản phẩm thắng cả tháng
    → phải cộng dồn theo sản phẩm, không chỉ nhìn dòng lớn nhất.
  - Headphones bán được nhiều cái nhất mỗi tháng nhưng doanh thu thấp
    → không được nhầm "số lượng" với "doanh thu".

Chạy:  python setup_demo.py
"""

import csv
from collections import defaultdict
from pathlib import Path

SANDBOX_DIR = Path(__file__).resolve().parent / "sandbox"

UNIT_PRICES = {  # đơn giá, VND
    "Laptop": 18_500_000,
    "Phone": 9_900_000,
    "Tablet": 7_200_000,
    "Monitor": 4_300_000,
    "Headphones": 1_250_000,
}

ORDERS = [  # (ngày, sản phẩm, số lượng)
    ("2026-01-03", "Laptop", 3), ("2026-01-05", "Phone", 3), ("2026-01-08", "Headphones", 10),
    ("2026-01-10", "Tablet", 2), ("2026-01-12", "Phone", 4), ("2026-01-15", "Monitor", 3),
    ("2026-01-18", "Phone", 2), ("2026-01-21", "Tablet", 1), ("2026-01-25", "Headphones", 6),
    ("2026-01-29", "Monitor", 2),
    ("2026-02-02", "Tablet", 5), ("2026-02-04", "Laptop", 2), ("2026-02-07", "Monitor", 4),
    ("2026-02-09", "Tablet", 4), ("2026-02-11", "Phone", 3), ("2026-02-14", "Headphones", 12),
    ("2026-02-17", "Tablet", 3), ("2026-02-20", "Laptop", 1), ("2026-02-23", "Monitor", 2),
    ("2026-02-26", "Headphones", 8),
    ("2026-03-01", "Phone", 2), ("2026-03-04", "Laptop", 2), ("2026-03-06", "Monitor", 5),
    ("2026-03-09", "Laptop", 3), ("2026-03-12", "Phone", 5), ("2026-03-15", "Headphones", 15),
    ("2026-03-18", "Tablet", 2), ("2026-03-22", "Phone", 3), ("2026-03-26", "Monitor", 6),
    ("2026-03-30", "Laptop", 1),
]


def sales_rows() -> list[dict]:
    """30 dòng dữ liệu bán hàng, mỗi dòng là một dict."""
    return [
        {"order_id": i, "date": date, "product": product, "quantity": qty, "unit_price": UNIT_PRICES[product]}
        for i, (date, product, qty) in enumerate(ORDERS, start=1)
    ]


def write_sales_csv(path: Path) -> None:
    rows = sales_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def top_product_by_month(rows: list[dict]) -> dict[str, tuple[str, int]]:
    """Đáp án đúng, ví dụ {"2026-01": ("Phone", 89_100_000), ...}."""
    revenue = defaultdict(int)  # (tháng, sản phẩm) → tổng doanh thu
    for r in rows:
        revenue[(r["date"][:7], r["product"])] += r["quantity"] * r["unit_price"]
    best = {}
    for (month, product), total in sorted(revenue.items()):
        if month not in best or total > best[month][1]:
            best[month] = (product, total)
    return best


if __name__ == "__main__":
    write_sales_csv(SANDBOX_DIR / "sales.csv")
    print(f"Đã tạo sandbox/sales.csv ({len(ORDERS)} dòng).")
    print("Đáp án đúng, để bạn đối chiếu với report.md của agent:")
    for month, (product, total) in top_product_by_month(sales_rows()).items():
        print(f"  {month}: {product:<10} {total:>13,} VND")
