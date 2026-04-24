#!/usr/bin/env python3
"""
企业微信「每日招中标速递」摘要：标题与正文格式（与业务约定一致）。

标题与统计区间：始终以库中 MAX(created_at/updated_at) 所在「入库日」为准（与披露页 /api/latest_date 一致）。
企微卡片链接为「摘要包」：materialize_digest_pack 写入 digest_packs/<token>/manifest.json，
卡片 URL 为 {disclose_base}/digest/<token>，不可猜测 token，避免改 URL 日期枚举全库。
date_start / date_end 参数保留兼容调用方，不参与统计计算。
正文：📌 今日概览 + 招标/中标条数。
仅统计「审核通过」记录；条数含当日新入库或当日更新（如待审核补全后）的记录，与披露列表一致。
"""

from __future__ import annotations

import configparser
import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def format_date_dots(ymd: str) -> str:
    """YYYY-MM-DD -> YYYY.MM.DD（月日两位）。"""
    ymd = (ymd or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", ymd)
    if not m:
        return ymd
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}.{int(mo):02d}.{int(d):02d}"


def build_digest_title(date_start: str, date_end: str) -> str:
    """标题日期固定取 start_date。"""
    a = (date_start or "").strip()
    mid = format_date_dots(a) if a else ""
    suffix = "全国最新招中标信息汇总"
    if mid:
        return f"【每日招中标速递】{mid} {suffix}"
    return f"【每日招中标速递】{suffix}"


def append_disclose_date_query(
    url: str, date_start: str, date_end: Optional[str] = None
) -> str:
    """
    在披露页 base URL 上附加 date_start / date_end（YYYY-MM-DD），
    与 web-view-node 列表页 query 一致，便于企微历史卡片点开仍对应当日数据。
    """
    base = (url or "").strip()
    ds = (date_start or "").strip()
    de = (date_end or ds or "").strip()
    if not base or not ds or not re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        return base
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", de):
        de = ds
    p = urlparse(base)
    qd = dict(parse_qsl(p.query, keep_blank_values=True))
    qd["date_start"] = ds
    qd["date_end"] = de
    new_q = urlencode(list(qd.items()))
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))


def digest_pack_root() -> Path:
    """与 web-view-node 共用：项目根下 digest_packs/。"""
    return Path(__file__).resolve().parent / "digest_packs"


def build_digest_pack_card_url(base_url: str, token: str) -> str:
    """企微 textcard 跳转：{base}/digest/{token}（base 不要尾斜杠）。"""
    u = (base_url or "").strip().rstrip("/")
    tok = (token or "").strip()
    if not u or not tok:
        return (base_url or "").strip()
    return f"{u}/digest/{tok}"


def _manifest_row_value(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return v


def materialize_digest_pack(config_path: Path, digest_date: str) -> str:
    """
    将「与摘要同一入库日、审核通过」的列表行写入 digest_packs/<token>/manifest.json。
    与 web-view-node /api/list 在仅按 created_at/updated_at 日期窗筛选时口径一致（不限 sub_type）。
    返回 token（32 位 hex），供 build_digest_pack_card_url 使用。
    """
    ds = (digest_date or "").strip()
    if not ds or not re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        raise ValueError("digest_date 须为 YYYY-MM-DD")
    de = ds
    token = secrets.token_hex(16)
    pack_dir = digest_pack_root() / token
    pack_dir.mkdir(parents=True, exist_ok=True)

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("database"):
        raise RuntimeError("config.ini 缺少 [database]")
    db = cfg["database"]
    import pymysql

    approved_sql = (
        "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = '审核通过'"
    )
    sql = f"""
        SELECT id, title, sub_type, product_related, reserve2 AS product_related_terms,
               project_no, project_budget, winning_amount, bidding_method,
               project_owner, owner_contact, owner_phone,
               winning_bidder, bidding_agent,
               published_at, bid_deadline, detail_url, created_at, audit_status
        FROM scraping_infos
        WHERE {approved_sql}
        AND (
          (DATE(created_at) >= %s AND DATE(created_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
          OR (DATE(updated_at) >= %s AND DATE(updated_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
        )
        ORDER BY id DESC
        LIMIT 8000
    """
    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ds, de, ds, de))
            rows = cur.fetchall() or []
    finally:
        conn.close()

    items = [{k: _manifest_row_value(v) for k, v in r.items()} for r in rows]
    manifest = {
        "digest_date": ds,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "items": items,
    }
    (pack_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return token


def get_digest_payload_for_ingest_date(config_path: Path, ymd: str) -> Tuple[str, str, str]:
    """
    按**指定入库日** YYYY-MM-DD 统计摘要（与 get_digest_payload 同一套 SQL，但不取 MAX 日）。
    用于测试：对前天/昨天/今天各发一条卡片。
    返回 (title, description, digest_link_date)，digest_link_date 恒为 ymd。
    """
    ds = (ymd or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        raise ValueError("ymd 须为 YYYY-MM-DD")
    de = ds

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("database"):
        raise RuntimeError("config.ini 缺少 [database]")

    db = cfg["database"]
    import pymysql

    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
    )
    approved_sql = (
        "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = '审核通过'"
    )

    try:
        cur = conn.cursor()
        title = build_digest_title(ds, de)

        cur.execute(
            f"""
            SELECT sub_type, COUNT(*) AS c FROM scraping_infos
            WHERE {approved_sql}
            AND sub_type IN ('招标公告', '中标结果')
            AND (
              (DATE(created_at) >= %s AND DATE(created_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
              OR (DATE(updated_at) >= %s AND DATE(updated_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
            )
            GROUP BY sub_type
            """,
            (ds, de, ds, de),
        )
        zbb, zb = 0, 0
        for st, c in cur.fetchall() or []:
            if st == "招标公告":
                zbb = int(c)
            elif st == "中标结果":
                zb = int(c)

        desc_lines = [
            "📌 今日概览",
            f"新增招标公告：{zbb} 条",
            f"新增中标公示：{zb} 条",
        ]
        return title, "\n".join(desc_lines), ds
    finally:
        conn.close()


def get_digest_payload(
    config_path: Path,
    date_start: Optional[str],
    date_end: Optional[str] = None,
) -> Tuple[str, str, str]:
    """
    从库中按「最新入库日」统计，生成 (title, description, digest_link_date)。
    digest_link_date：与摘要统计一致的 YYYY-MM-DD，用于生成当日摘要包 manifest。
    入库日 = DATE(MAX(created_at/updated_at))；无数据时标题用本机当天、条数为 0。
    date_start / date_end 参数保留兼容调用方，不参与统计计算。
    """
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("database"):
        raise RuntimeError("config.ini 缺少 [database]")

    db = cfg["database"]
    import pymysql

    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
    )
    approved_sql = (
        "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = '审核通过'"
    )

    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT MAX(d) FROM (
              SELECT DATE(created_at) AS d FROM scraping_infos WHERE {approved_sql}
              UNION ALL
              SELECT DATE(updated_at) AS d FROM scraping_infos WHERE {approved_sql}
            ) x WHERE d IS NOT NULL
            """
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            t = datetime.now().strftime("%Y-%m-%d")
            title = build_digest_title(t, t)
            desc = (
                "📌 今日概览\n"
                "新增招标公告：0 条\n"
                "新增中标公示：0 条"
            )
            return title, desc, t

        d = row[0]
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d).split()[0]
        de = ds

        title = build_digest_title(ds, de)

        # 查询上界多加1天，兼容跨日入库；含当日 created_at 或 updated_at 落在区间内的审核通过记录
        cur.execute(
            f"""
            SELECT sub_type, COUNT(*) AS c FROM scraping_infos
            WHERE {approved_sql}
            AND sub_type IN ('招标公告', '中标结果')
            AND (
              (DATE(created_at) >= %s AND DATE(created_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
              OR (DATE(updated_at) >= %s AND DATE(updated_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
            )
            GROUP BY sub_type
            """,
            (ds, de, ds, de),
        )
        zbb, zb = 0, 0
        for st, c in cur.fetchall() or []:
            if st == "招标公告":
                zbb = int(c)
            elif st == "中标结果":
                zb = int(c)

        desc_lines = [
            "📌 今日概览",
            f"新增招标公告：{zbb} 条",
            f"新增中标公示：{zb} 条",
        ]
        return title, "\n".join(desc_lines), ds
    finally:
        conn.close()
