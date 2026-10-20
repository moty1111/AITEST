"""按 order_id 去重 sourcing_orders.csv，输出一份新的 CSV。"""

from __future__ import annotations

import csv
from pathlib import Path

SRC = Path(__file__).with_name("sourcing_orders.csv")
DST = Path(__file__).with_name("sourcing_orders_deduped.csv")
KEY = "order_id"


def main() -> None:
    with SRC.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if not fieldnames or KEY not in fieldnames:
            raise SystemExit(f"源文件缺少字段 {KEY}: {SRC}")
        seen: set[str] = set()
        unique_rows: list[dict[str, str]] = []
        duplicate_count = 0
        for row in reader:
            order_id = (row.get(KEY) or "").strip()
            if not order_id:
                unique_rows.append(row)
                continue
            if order_id in seen:
                duplicate_count += 1
                continue
            seen.add(order_id)
            unique_rows.append(row)

    with DST.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique_rows)

    print(f"源文件: {SRC}")
    print(f"输出文件: {DST}")
    print(f"去重后行数: {len(unique_rows)}")
    print(f"去掉的重复行: {duplicate_count}")
    print(f"唯一 order_id 数: {len(seen)}")


if __name__ == "__main__":
    main()
