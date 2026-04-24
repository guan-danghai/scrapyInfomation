#!/usr/bin/env python3
"""
供流水线使用：按「入库日」查询仍为待审核的记录，供爬虫按 detail_url 直开详情补抓。

与 ingest_bank_to_db 共用 config.ini [database]。
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any


def _load_db_cfg(config_path: Path) -> dict[str, Any] | None:
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("database"):
        return None
    d = cfg["database"]
    return {
        "host": d.get("host", "127.0.0.1"),
        "port": int(d.get("port", 3306)),
        "database": d.get("database", ""),
        "user": d.get("user", "root"),
        "password": d.get("password", ""),
        "charset": d.get("charset", "utf8mb4"),
    }


def fetch_pending_urls_for_reaudit(config_path: Path, created_date: str) -> list[dict[str, str]]:
    """
    查询：audit_status = 待审核 且 DATE(created_at) = created_date（YYYY-MM-DD）。
    返回 [{detail_url, keyword, sub_type, title}, ...]，按 detail_url 去重（保留第一条）。
    若表无 audit_status 列或查询失败，返回 []。
    """
    created_date = (created_date or "").strip()[:10]
    if len(created_date) != 10:
        return []

    db = _load_db_cfg(config_path)
    if not db or not db.get("database"):
        return []

    import pymysql

    try:
        conn = pymysql.connect(
            host=db["host"],
            port=db["port"],
            user=db["user"],
            password=db["password"],
            database=db["database"],
            charset=db["charset"],
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception:
        return []

    rows: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT detail_url, keyword, sub_type, title
                FROM scraping_infos
                WHERE TRIM(IFNULL(audit_status, '')) = '待审核'
                  AND DATE(created_at) = %s
                  AND detail_url IS NOT NULL
                  AND TRIM(detail_url) <> ''
                ORDER BY id ASC
                """,
                (created_date,),
            )
            rows = list(cur.fetchall() or [])
    except Exception:
        rows = []
    finally:
        conn.close()

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for r in rows:
        u = (r.get("detail_url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(
            {
                "detail_url": u,
                "keyword": (r.get("keyword") or "银行").strip() or "银行",
                "sub_type": (r.get("sub_type") or "采招信息").strip() or "采招信息",
                "title": (r.get("title") or "").strip(),
            }
        )
    return out
