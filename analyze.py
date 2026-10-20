"""对跨境电商采购订单执行质量检查、失败率分组和预警规则评估。

只使用 Python 标准库。默认读取原始 sourcing_orders.csv，在内存中按 order_id
保留第一条记录，以便同时检查原始文件的重复订单并复现去重后的分析口径。
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "sourcing_orders.csv"
REPORT_FILE = BASE_DIR / "analysis_results.md"
RULE_METRICS_FILE = BASE_DIR / "rule_evaluation.csv"
REQUIRED_COLUMNS = (
    "order_id",
    "order_time",
    "last_synced_at",
    "team",
    "category",
    "supplier_stock",
    "trend_score",
    "is_fulfilled",
)
FAILURE_LOSS_YUAN = 120
ALERT_COST_YUAN = 20


@dataclass(frozen=True)
class Order:
    order_id: str
    order_time: datetime
    last_synced_at: datetime
    team: str
    category: str
    supplier_stock: int
    trend_score: int
    is_fulfilled: int | None

    @property
    def sync_lag_minutes(self) -> float:
        return (self.order_time - self.last_synced_at).total_seconds() / 60


def parse_timestamp(value: str) -> datetime:
    """将 ISO 8601 字符串解析为带时区的 datetime。"""
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return datetime.fromisoformat(value)


def percentage(numerator: int, denominator: int) -> str:
    return "—" if denominator == 0 else f"{numerator / denominator * 100:.2f}%"


def markdown_table(headers: list[str], rows: Iterable[list[str]]) -> str:
    lines = [
        f"| {' | '.join(headers)} |",
        f"| {' | '.join(['---:'] * len(headers))} |",
    ]
    for row in rows:
        lines.append(f"| {' | '.join(row)} |")
    return "\n".join(lines)


def read_and_validate(
    source: Path,
) -> tuple[list[Order], dict[str, int], dict[str, list[str]]]:
    """读取原始文件、检查质量，并在内存中按 order_id 去重。"""
    quality_counts = Counter()
    issue_samples: dict[str, list[str]] = defaultdict(list)

    with source.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError("CSV 缺少表头。")
        missing_columns = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
        if missing_columns:
            raise ValueError(f"CSV 缺少必需字段：{', '.join(missing_columns)}")
        raw_rows = list(reader)

    quality_counts["raw_rows"] = len(raw_rows)
    seen_ids: set[str] = set()
    unique_rows: list[dict[str, str]] = []

    for line_number, row in enumerate(raw_rows, start=2):
        order_id = (row["order_id"] or "").strip()
        if not order_id:
            quality_counts["missing_order_id"] += 1
            issue_samples["缺失订单号"].append(str(line_number))
            # 空订单号无法判断是否同一业务单，保留并在后续继续检查。
            unique_rows.append(row)
            continue
        if order_id in seen_ids:
            quality_counts["duplicate_rows"] += 1
            issue_samples["重复订单号"].append(f"{order_id}（第 {line_number} 行）")
            continue
        seen_ids.add(order_id)
        unique_rows.append(row)

    quality_counts["deduplicated_rows"] = len(unique_rows)
    orders: list[Order] = []

    for row in unique_rows:
        order_id = (row["order_id"] or "").strip()
        required_values = {
            name: (row[name] or "").strip()
            for name in REQUIRED_COLUMNS
            if name != "is_fulfilled"
        }
        for column, value in required_values.items():
            if not value:
                quality_counts[f"missing_{column}"] += 1
                issue_samples["关键字段缺失"].append(f"{order_id or '空订单号'}:{column}")

        fulfilled_value = (row["is_fulfilled"] or "").strip()
        if not fulfilled_value:
            quality_counts["missing_is_fulfilled"] += 1
        elif fulfilled_value not in {"0", "1"}:
            quality_counts["invalid_is_fulfilled"] += 1
            issue_samples["非法履约状态"].append(
                f"{order_id or '空订单号'}:{fulfilled_value}"
            )

        try:
            order_time = parse_timestamp(required_values["order_time"])
            last_synced_at = parse_timestamp(required_values["last_synced_at"])
        except (KeyError, ValueError) as error:
            quality_counts["invalid_timestamp"] += 1
            issue_samples["无法解析时间"].append(f"{order_id or '空订单号'}:{error}")
            continue

        try:
            supplier_stock = int(required_values["supplier_stock"])
            trend_score = int(required_values["trend_score"])
        except (KeyError, ValueError) as error:
            quality_counts["invalid_numeric_value"] += 1
            issue_samples["无法解析数值"].append(f"{order_id or '空订单号'}:{error}")
            continue

        lag = (order_time - last_synced_at).total_seconds() / 60
        if lag < 0:
            quality_counts["sync_after_order"] += 1
            issue_samples["同步时间晚于下单时间"].append(order_id)
        if lag > 24 * 60:
            quality_counts["sync_lag_over_24_hours"] += 1
            issue_samples["同步时延超过 24 小时"].append(order_id)
        if supplier_stock < 0:
            quality_counts["negative_supplier_stock"] += 1
            issue_samples["负库存"].append(order_id)
        if not 0 <= trend_score <= 100:
            quality_counts["trend_score_out_of_range"] += 1
            issue_samples["热度分数越界"].append(order_id)

        if (
            not order_id
            or any(not value for value in required_values.values())
            or fulfilled_value not in {"", "0", "1"}
        ):
            continue

        orders.append(
            Order(
                order_id=order_id,
                order_time=order_time,
                last_synced_at=last_synced_at,
                team=required_values["team"],
                category=required_values["category"],
                supplier_stock=supplier_stock,
                trend_score=trend_score,
                is_fulfilled=None if not fulfilled_value else int(fulfilled_value),
            )
        )

    quality_counts["valid_orders"] = len(orders)
    return orders, dict(quality_counts), issue_samples


def group_failure_rates(
    orders: Iterable[Order],
    bucket: Callable[[Order], str],
    ordering: list[str] | None = None,
) -> list[list[str]]:
    """仅用已回传订单计算每个分组的失败率。"""
    grouped: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for order in orders:
        if order.is_fulfilled is None:
            continue
        values = grouped[bucket(order)]
        values[0] += 1
        values[1] += int(order.is_fulfilled == 0)

    total_known = sum(values[0] for values in grouped.values())
    total_failures = sum(values[1] for values in grouped.values())
    overall_rate = total_failures / total_known
    items = list(grouped.items())
    if ordering:
        rank = {value: index for index, value in enumerate(ordering)}
        items.sort(key=lambda item: rank[item[0]])
    else:
        items.sort(key=lambda item: item[0])

    return [
        [
            name,
            str(known),
            str(failures),
            percentage(failures, known),
            f"{(failures / known) / overall_rate:.2f}×",
        ]
        for name, (known, failures) in items
    ]


def sync_lag_bucket(order: Order) -> str:
    lag = order.sync_lag_minutes
    if lag < 10:
        return "0–9 分钟"
    if lag < 30:
        return "10–29 分钟"
    if lag < 45:
        return "30–44 分钟"
    return "45–60 分钟"


def stock_bucket(order: Order) -> str:
    stock = order.supplier_stock
    if stock == 0:
        return "0"
    if stock <= 5:
        return "1–5"
    if stock <= 10:
        return "6–10"
    if stock <= 20:
        return "11–20"
    return "21–30"


def trend_bucket(order: Order) -> str:
    score = order.trend_score
    if score < 40:
        return "0–39"
    if score < 60:
        return "40–59"
    if score < 70:
        return "60–69"
    if score < 80:
        return "70–79"
    return "80–100"


def hour_bucket(order: Order) -> str:
    hour = order.order_time.hour
    if hour < 9:
        return "07:00–08:59"
    if hour < 12:
        return "09:00–11:59"
    if hour < 15:
        return "12:00–14:59"
    if hour < 18:
        return "15:00–17:59"
    return "18:00–21:59"


def evaluate_rule(
    name: str, orders: list[Order], predicate: Callable[[Order], bool]
) -> dict[str, str | int]:
    """按规则计算命中、覆盖、误报和反事实经济价值。"""
    alerted = [order for order in orders if predicate(order)]
    known_alerted = [order for order in alerted if order.is_fulfilled is not None]
    unknown_alerted = [order for order in alerted if order.is_fulfilled is None]
    hit_failures = sum(order.is_fulfilled == 0 for order in known_alerted)
    false_positives = sum(order.is_fulfilled == 1 for order in known_alerted)
    all_known_failures = sum(
        order.is_fulfilled == 0 for order in orders if order.is_fulfilled is not None
    )
    alert_cost = len(alerted) * ALERT_COST_YUAN
    theoretical_avoided_loss = hit_failures * FAILURE_LOSS_YUAN
    theoretical_net_benefit = theoretical_avoided_loss - alert_cost

    return {
        "规则": name,
        "预警订单数（全部）": len(alerted),
        "已回传预警订单数": len(known_alerted),
        "未回传预警订单数": len(unknown_alerted),
        "已知失败命中数": hit_failures,
        "预警命中率（仅已回传）": percentage(hit_failures, len(known_alerted)),
        "失败订单覆盖率": percentage(hit_failures, all_known_failures),
        "误报数（已知成功）": false_positives,
        "预警成本（元）": alert_cost,
        "理论避免损失（元）": theoretical_avoided_loss,
        "理论净收益（元）": theoretical_net_benefit,
    }


def write_rule_metrics_csv(metrics: list[dict[str, str | int]]) -> None:
    headers = list(metrics[0])
    with RULE_METRICS_FILE.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(metrics)


def create_report(
    orders: list[Order],
    quality_counts: dict[str, int],
    issue_samples: dict[str, list[str]],
    rule_metrics: list[dict[str, str | int]],
) -> None:
    known_orders = [order for order in orders if order.is_fulfilled is not None]
    failures = sum(order.is_fulfilled == 0 for order in known_orders)
    successes = len(known_orders) - failures
    unknown = len(orders) - len(known_orders)
    headers = ["分组", "已回传订单数", "失败订单数", "失败率", "相对总体倍数"]
    rule_headers = list(rule_metrics[0])

    sections = [
        "# 采购订单失败与预警规则分析结果",
        "",
        f"> 输入：`{INPUT_FILE.name}`；生成时间：由 `analyze.py` 运行时计算。",
        "",
        "## 数据质量检查",
        "",
        markdown_table(
            ["检查项", "数量"],
            [
                ["原始数据行数", str(quality_counts.get("raw_rows", 0))],
                ["重复订单行（按 order_id）", str(quality_counts.get("duplicate_rows", 0))],
                ["内存去重后行数", str(quality_counts.get("deduplicated_rows", 0))],
                ["有效订单数", str(quality_counts.get("valid_orders", 0))],
                ["缺失 order_id", str(quality_counts.get("missing_order_id", 0))],
                ["缺失下单时间", str(quality_counts.get("missing_order_time", 0))],
                [
                    "缺失库存同步时间",
                    str(quality_counts.get("missing_last_synced_at", 0)),
                ],
                ["缺失团队", str(quality_counts.get("missing_team", 0))],
                ["缺失类目", str(quality_counts.get("missing_category", 0))],
                [
                    "缺失供应商库存",
                    str(quality_counts.get("missing_supplier_stock", 0)),
                ],
                ["缺失商品热度", str(quality_counts.get("missing_trend_score", 0))],
                ["缺失履约结果", str(quality_counts.get("missing_is_fulfilled", 0))],
                ["非法履约结果", str(quality_counts.get("invalid_is_fulfilled", 0))],
                ["无法解析时间", str(quality_counts.get("invalid_timestamp", 0))],
                ["同步时间晚于下单时间", str(quality_counts.get("sync_after_order", 0))],
                ["同步时延超过 24 小时", str(quality_counts.get("sync_lag_over_24_hours", 0))],
                ["负库存", str(quality_counts.get("negative_supplier_stock", 0))],
                ["热度分数越界（非 0–100）", str(quality_counts.get("trend_score_out_of_range", 0))],
            ],
        ),
        "",
        "> 结果未回传不等价于成功或失败：它们保留在预警成本中，但不参与失败率、命中率和覆盖率计算。",
        "",
        "## 总体采购结果",
        "",
        markdown_table(
            ["指标", "数值"],
            [
                ["有效订单数（去重后）", str(len(orders))],
                ["已回传结果订单数", str(len(known_orders))],
                ["成功订单数", str(successes)],
                ["失败订单数", str(failures)],
                ["整体采购失败率", percentage(failures, len(known_orders))],
                ["未回传订单数", str(unknown)],
            ],
        ),
        "",
        "## 分组失败率",
        "",
        "### 库存同步时延（order_time − last_synced_at）",
        "",
        markdown_table(
            headers,
            group_failure_rates(
                orders,
                sync_lag_bucket,
                ["0–9 分钟", "10–29 分钟", "30–44 分钟", "45–60 分钟"],
            ),
        ),
        "",
        "### 供应商库存（supplier_stock）",
        "",
        markdown_table(
            headers,
            group_failure_rates(
                orders, stock_bucket, ["0", "1–5", "6–10", "11–20", "21–30"]
            ),
        ),
        "",
        "### 商品热度（trend_score）",
        "",
        markdown_table(
            headers,
            group_failure_rates(
                orders,
                trend_bucket,
                ["0–39", "40–59", "60–69", "70–79", "80–100"],
            ),
        ),
        "",
        "### 发起采购团队（team）",
        "",
        markdown_table(headers, group_failure_rates(orders, lambda order: order.team)),
        "",
        "### 货品类目（category）",
        "",
        markdown_table(headers, group_failure_rates(orders, lambda order: order.category)),
        "",
        "### 下单时段（order_time，北京时间）",
        "",
        markdown_table(
            headers,
            group_failure_rates(
                orders,
                hour_bucket,
                [
                    "07:00–08:59",
                    "09:00–11:59",
                    "12:00–14:59",
                    "15:00–17:59",
                    "18:00–21:59",
                ],
            ),
        ),
        "",
        "## 预警规则比较",
        "",
        "### 规则定义",
        "",
        "- **规则 A**：库存同步时延达到 10 分钟即预警。",
        "- **规则 B**：库存同步时延达到 10 分钟，且（商品热度达到 70 分 **或** 供应商库存不超过 5 件）时预警。",
        "",
        markdown_table(
            rule_headers,
            [[str(metric[header]) for header in rule_headers] for metric in rule_metrics],
        ),
        "",
        "### 经济价值口径",
        "",
        f"- 每笔采购失败损失：{FAILURE_LOSS_YUAN} 元；每笔触发预警的处理成本：{ALERT_COST_YUAN} 元。",
        "- 预警成本 = 全部预警订单数 × 20 元，包含结果未回传订单。",
        "- **理论避免损失** = 已知失败命中数 × 120 元；它假设预警后的复核或拦截能够 100% 避免该失败，因此是反事实理论估算，不是实际已实现收益。",
        "- **理论净收益** = 理论避免损失 − 预警成本；未回传订单不计入命中或误报，但其预警成本已计入。",
        "",
        "### 结论",
        "",
        f"- 规则 A 的预警覆盖更广；规则 B 是规则 A 的收紧子集，预警量更少。是否更好必须比较命中率、覆盖率和理论净收益，不能先验认定。",
        f"- 本次已回传订单的整体失败率为 {percentage(failures, len(known_orders))}。库存同步时延分组的结果可直接复现此前报告。",
        "- 这些结果描述统计关联，不足以单独证明某字段对失败存在因果影响；投入生产前应在新的时间段验证，并跟踪预警后的真实拦截结果。",
        "",
    ]
    REPORT_FILE.write_text("\n".join(sections), encoding="utf-8")

    if issue_samples:
        # 仅在终端展示有限样例，避免将可能很长的明细塞进报告。
        print("质量问题样例（每类最多 3 个）：")
        for category, samples in issue_samples.items():
            print(f"  - {category}: {', '.join(samples[:3])}")


def main() -> None:
    orders, quality_counts, issue_samples = read_and_validate(INPUT_FILE)
    rules = [
        (
            "规则 A：时延 ≥10 分钟",
            lambda order: order.sync_lag_minutes >= 10,
        ),
        (
            "规则 B：时延 ≥10 分钟且（热度 ≥70 或库存 ≤5）",
            lambda order: order.sync_lag_minutes >= 10
            and (order.trend_score >= 70 or order.supplier_stock <= 5),
        ),
    ]
    rule_metrics = [
        evaluate_rule(name, orders, predicate) for name, predicate in rules
    ]
    write_rule_metrics_csv(rule_metrics)
    create_report(orders, quality_counts, issue_samples, rule_metrics)

    known_orders = [order for order in orders if order.is_fulfilled is not None]
    failures = sum(order.is_fulfilled == 0 for order in known_orders)
    print(f"有效订单：{len(orders)}；重复订单行：{quality_counts.get('duplicate_rows', 0)}")
    print(
        f"已回传订单：{len(known_orders)}；失败：{failures}；"
        f"整体失败率：{percentage(failures, len(known_orders))}"
    )
    for metric in rule_metrics:
        print(
            f"{metric['规则']}：预警 {metric['预警订单数（全部）']} 单，"
            f"命中率 {metric['预警命中率（仅已回传）']}，"
            f"覆盖率 {metric['失败订单覆盖率']}，"
            f"理论净收益 {metric['理论净收益（元）']} 元"
        )
    print(f"已写入：{REPORT_FILE.name}、{RULE_METRICS_FILE.name}")


if __name__ == "__main__":
    main()
