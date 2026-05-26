#!/usr/bin/env python3
"""
从 crm_lead（或 config [dispatch] 配置的线索表）读取客户与销售（售前）对应关系。

规则：
- 时间顺序与 dispatch_router 一致（lead_time_order_col / register_date / updated_at / id 等从新到旧）；
- 同一 TRIM(customer_name) 若有多条线索：**优先取时间上最近的一条且 sales 非空**；
  若最近若干条 sales 都为空，则继续往前找 **最近一条 sales 非空**；
  若该客户所有线索 sales 均为空，则 sales 输出空字符串，lead_row_id 仍指向该客户**最新一条**线索。

用法：
  python tools/crm_lead_latest_sales_per_customer.py
  python tools/crm_lead_latest_sales_per_customer.py -o customer_latest_sales.csv
  python tools/crm_lead_latest_sales_per_customer.py --format txt -o out.txt
  python tools/crm_lead_latest_sales_per_customer.py --json
"""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatch_router import (  # noqa: E402
    DispatchConfig,
    _lead_business_line_sql_condition,
    _lead_columns,
    _lead_order_expression,
    load_dispatch_config,
)


def _build_where(cols: set[str], dcfg: DispatchConfig) -> str:
    parts: list[str] = []
    if dcfg.lead_enabled_col in cols:
        parts.append(f"(`{dcfg.lead_enabled_col}`=1 OR `{dcfg.lead_enabled_col}` IS NULL)")
    parts.append(f"`{dcfg.lead_customer_col}` IS NOT NULL AND TRIM(`{dcfg.lead_customer_col}`)<>''")
    bc = _lead_business_line_sql_condition(cols, dcfg)
    if bc:
        parts.append(bc)
    return "WHERE " + " AND ".join(parts)


def _full_order_by(cols: set[str], dcfg: DispatchConfig) -> str:
    base = _lead_order_expression(cols, dcfg)
    if "id" in cols:
        return f"{base}, `id` DESC"
    return base


def _select_columns(cols: set[str], dcfg: DispatchConfig) -> tuple[list[str], list[str]]:
    """返回 SELECT 片段列表（含 AS customer_name / sales）以及用于排序的字段名集合。"""
    parts: list[str] = [
        f"TRIM(`{dcfg.lead_customer_col}`) AS `customer_name`",
        f"`{dcfg.lead_presale_names_col}` AS `sales`",
    ]
    order_fields: list[str] = []
    if dcfg.lead_time_order_col and dcfg.lead_time_order_col in cols:
        parts.append(f"`{dcfg.lead_time_order_col}`")
        order_fields.append(dcfg.lead_time_order_col)
    if "register_date" in cols and "register_date" not in order_fields:
        parts.append("`register_date`")
        order_fields.append("register_date")
    for c in ("updated_at", "created_at", "id"):
        if c in cols:
            parts.append(f"`{c}`")
            if c not in order_fields:
                order_fields.append(c)
    return parts, order_fields


def _coerce_ts(v) -> datetime:
    if v is None:
        return datetime.min
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
    return datetime.min


def _row_sort_tuple(r: dict, cols: set[str], dcfg: DispatchConfig) -> tuple:
    """与 _lead_order_expression 一致：越大表示越新。"""
    t_raw = None
    if dcfg.lead_time_order_col and dcfg.lead_time_order_col in cols:
        t_raw = r.get(dcfg.lead_time_order_col)
    elif "register_date" in cols:
        t_raw = r.get("register_date")
    elif "updated_at" in cols and "created_at" in cols:
        t_raw = r.get("updated_at") or r.get("created_at")
    elif "updated_at" in cols:
        t_raw = r.get("updated_at")
    elif "created_at" in cols:
        t_raw = r.get("created_at")
    ts = _coerce_ts(t_raw)
    rid = int(r["id"]) if "id" in cols and r.get("id") is not None else 0
    return (ts, rid)


def fetch_sales_per_customer(conn, dcfg: DispatchConfig) -> list[dict]:
    cols = _lead_columns(conn, dcfg)
    if dcfg.lead_customer_col not in cols:
        raise RuntimeError(f"表 `{dcfg.lead_table}` 缺少客户列 `{dcfg.lead_customer_col}`")
    if dcfg.lead_presale_names_col not in cols:
        raise RuntimeError(f"表 `{dcfg.lead_table}` 缺少售前姓名列 `{dcfg.lead_presale_names_col}`")

    select_parts, _ = _select_columns(cols, dcfg)
    where_sql = _build_where(cols, dcfg)
    order_sql = _full_order_by(cols, dcfg)
    sql = f"SELECT {', '.join(select_parts)} FROM `{dcfg.lead_table}` {where_sql} ORDER BY {order_sql}"

    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []

    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name = (r.get("customer_name") or "").strip()
        if name:
            by_name[name].append(r)

    out: list[dict] = []
    for name in sorted(by_name.keys()):
        lst = by_name[name]
        lst.sort(key=lambda x: _row_sort_tuple(x, cols, dcfg), reverse=True)

        newest = lst[0]
        chosen_sales_row: dict | None = None
        sales_val = ""
        for r in lst:
            s = (r.get("sales") or "").strip()
            if s:
                chosen_sales_row = r
                sales_val = s
                break

        item: dict = {
            "customer_name": name,
            "sales": sales_val,
        }
        if "id" in cols:
            if chosen_sales_row is not None:
                item["lead_row_id"] = chosen_sales_row.get("id")
                item["sales_from_row_id"] = chosen_sales_row.get("id")
            else:
                item["lead_row_id"] = newest.get("id")
                item["sales_from_row_id"] = ""
        if "register_date" in cols:
            item["register_date_latest"] = (
                str(newest["register_date"]) if newest.get("register_date") is not None else ""
            )
            if chosen_sales_row is not None and chosen_sales_row.get("register_date") is not None:
                item["register_date_sales"] = str(chosen_sales_row.get("register_date"))
            else:
                item["register_date_sales"] = ""

        out.append(item)

    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("\ufeffcustomer_name,sales,lead_row_id,sales_from_row_id,register_date_latest,register_date_sales\n", encoding="utf-8-sig")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_txt(path: Path, rows: list[dict]) -> None:
    cols = [
        "customer_name",
        "sales",
        "lead_row_id",
        "sales_from_row_id",
        "register_date_latest",
        "register_date_sales",
    ]
    lines = ["\t".join(cols)]
    for r in rows:
        lines.append("\t".join(str(r.get(c) or "") for c in cols))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="crm_lead：每客户取最近非空 sales，导出 CSV/TXT/JSON")
    ap.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    ap.add_argument(
        "--format",
        choices=("csv", "txt"),
        default="csv",
        help="文件格式（默认 csv）",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出文件路径；默认：项目根目录 customer_latest_sales.csv 或 .txt",
    )
    args = ap.parse_args()

    cfg_path = ROOT / "config.ini"
    dcfg = load_dispatch_config(cfg_path)

    ini = configparser.ConfigParser()
    ini.read(cfg_path, encoding="utf-8")
    db = ini["database"]
    conn = pymysql.connect(
        host=db.get("host"),
        port=int(db.get("port")),
        user=db.get("user"),
        password=db.get("password"),
        database=db.get("database"),
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        rows = fetch_sales_per_customer(conn, dcfg)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    out_path = args.output
    if out_path is None:
        ext = "csv" if args.format == "csv" else "txt"
        out_path = ROOT / f"customer_latest_sales.{ext}"

    if args.format == "csv":
        _write_csv(out_path, rows)
    else:
        _write_txt(out_path, rows)

    print(str(out_path.resolve()), file=sys.stderr)


if __name__ == "__main__":
    main()
