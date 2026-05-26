#!/usr/bin/env python3
"""
按「入库日」重新对 scraping_infos 跑一遍 AI + build_scraping_info_row，写回数据库。
用于充值恢复后补全 project_owner 等字段。

用法:
  python tools/reanalyze_db_by_date.py                    # 默认今天 CURDATE()
  python tools/reanalyze_db_by_date.py --date 2026-05-07
  python tools/reanalyze_db_by_date.py --dry-run --limit 5
  python tools/reanalyze_db_by_date.py --empty-owner-only   # 仅补 project_owner 仍为空的（断点续跑）
环境变量: PYTHONUNBUFFERED=1 可避免管道无实时日志
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG_FILE = ROOT / "config.ini"

import ai_analyze
import ingest_bank_to_db as ing


def _analyze_with_timeout(title: str, content: str, timeout_sec: float):
    """避免单次 API 无限挂起阻塞整批任务。"""

    def _call():
        return ai_analyze.analyze_with_ai(title, content, CONFIG_FILE)

    if timeout_sec <= 0:
        return _call()
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_call)
        try:
            return fut.result(timeout=timeout_sec)
        except FuturesTimeout:
            return "__TIMEOUT__"


def main() -> int:
    ap = argparse.ArgumentParser(description="按日期批量重跑 AI 并更新 scraping_infos")
    ap.add_argument(
        "--date",
        dest="day",
        default="",
        help="入库日 YYYY-MM-DD，默认今天（本机日期）",
    )
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条，0 表示不限制")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不调用 API、不写库",
    )
    ap.add_argument("--sleep", type=float, default=0.15, help="每条之间休眠秒数，防限流")
    ap.add_argument(
        "--empty-owner-only",
        action="store_true",
        help="只处理 project_owner 仍为空的记录（大批量中断后续跑）",
    )
    ap.add_argument(
        "--ai-timeout",
        type=float,
        default=120.0,
        help="单条 AI 调用超时秒数，超时跳过该条（0=不限制）",
    )
    args = ap.parse_args()

    if args.day.strip():
        day = args.day.strip()
    else:
        day = date.today().isoformat()

    cfg = ing.load_config()
    db_cfg = cfg.get("db")
    if not db_cfg or not db_cfg.get("database"):
        print("未配置 database，退出", file=sys.stderr)
        return 1
    if not args.dry_run and not (cfg.get("ai_api_key") or "").strip():
        print("未配置 [ai] api_key，无法重跑 AI", file=sys.stderr)
        return 1

    import pymysql
    from pymysql.cursors import DictCursor

    conn = ing._mysql_connect(db_cfg)
    try:
        with conn.cursor(DictCursor) as cur:
            where = "DATE(created_at)=%s"
            params: list = [day]
            if args.empty_owner_only:
                where += " AND (project_owner IS NULL OR TRIM(project_owner)='')"
            cur.execute(
                "SELECT id, keyword, `type`, sub_type, title, detail, detail_url, "
                "audit_status, reserve1, project_owner "
                f"FROM scraping_infos WHERE {where} ORDER BY id ASC",
                tuple(params),
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()

    total = len(rows)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    print(
        f"入库日 {day} 命中（{'仅 owner 空' if args.empty_owner_only else '全部'}）{total} 条，将处理 {len(rows)} 条",
        flush=True,
    )

    empty_before = sum(
        1 for r in rows if not str(r.get("project_owner") or "").strip()
    )
    print(f"其中当前 project_owner 为空: {empty_before} 条", flush=True)

    if args.dry_run:
        print("[dry-run] 结束", flush=True)
        return 0

    conn = ing._mysql_connect(db_cfg)
    stats = {
        "ok": 0,
        "ai_fail": 0,
        "db_fail": 0,
        "owner_gained": 0,
        "owner_was_filled": 0,
    }

    try:
        for i, rec in enumerate(rows, 1):
            rid = int(rec["id"])
            title = (rec.get("title") or "").strip()
            content = (rec.get("detail") or "").strip()
            kw = (rec.get("keyword") or ing.DEFAULT_KEYWORD).strip() or ing.DEFAULT_KEYWORD
            info_type = (
                (rec.get("sub_type") or rec.get("type") or "采招信息").strip()
                or "采招信息"
            )
            url = (rec.get("detail_url") or "").strip()
            crawl_time = (rec.get("reserve1") or "").strip()
            saved_audit = rec.get("audit_status")
            old_owner = (rec.get("project_owner") or "").strip()
            old_empty = not old_owner

            if not content:
                print(f"[{i}/{len(rows)}] id={rid} 无正文，跳过", flush=True)
                stats["ai_fail"] += 1
                continue

            try:
                ai_result = _analyze_with_timeout(
                    title, content, args.ai_timeout
                )
            except Exception as e:
                print(f"[{i}/{len(rows)}] id={rid} AI 异常: {e}", flush=True)
                stats["ai_fail"] += 1
                continue

            if ai_result == "__TIMEOUT__":
                print(
                    f"[{i}/{len(rows)}] id={rid} AI 超时(>{args.ai_timeout}s)，跳过",
                    flush=True,
                )
                stats["ai_fail"] += 1
                continue

            if ai_result is None:
                print(
                    f"[{i}/{len(rows)}] id={rid} AI 返回 None（密钥/网络/解析失败）",
                    flush=True,
                )
                stats["ai_fail"] += 1
                continue

            raw_doc = {
                "title": title,
                "content": content,
                "url": url,
                "crawl_time": crawl_time,
                "info_type": info_type,
            }
            new_row = ai_analyze.build_scraping_info_row(
                raw_doc,
                keyword=kw,
                info_type=info_type,
                ai_result=ai_result,
            )
            # 不重算审核状态：避免已「审核通过」被短正文逻辑打回待审核
            if saved_audit is not None and str(saved_audit).strip():
                new_row["audit_status"] = saved_audit

            n = ing.update_row_by_id(conn, rid, new_row)
            if n:
                stats["ok"] += 1
                new_owner = (new_row.get("project_owner") or "").strip()
                if old_empty and new_owner:
                    stats["owner_gained"] += 1
                if not old_empty:
                    stats["owner_was_filled"] += 1
                if i <= 3 or (old_empty and new_owner):
                    tail = ""
                    if new_owner:
                        tail = (
                            f" 采购人: {new_owner[:40]}..."
                            if len(new_owner) > 40
                            else f" 采购人: {new_owner}"
                        )
                    print(f"[{i}/{len(rows)}] id={rid} 更新 OK{tail}", flush=True)
            else:
                stats["db_fail"] += 1
                print(f"[{i}/{len(rows)}] id={rid} 更新 0 行", flush=True)

            if args.sleep > 0:
                time.sleep(args.sleep)
    finally:
        conn.close()

    print("--- 汇总 ---", flush=True)
    print(f"成功更新: {stats['ok']} 条", flush=True)
    print(f"AI 失败/跳过: {stats['ai_fail']} 条", flush=True)
    print(f"写库失败: {stats['db_fail']} 条", flush=True)
    print(
        f"原空的 project_owner 本次补出非空: {stats['owner_gained']} 条 "
        f"（原已有采购人的条数: {stats['owner_was_filled']}）",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
