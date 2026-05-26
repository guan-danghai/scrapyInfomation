#!/usr/bin/env python3
"""
根据 digest_pending_send.json 中的 digest_date 与 token：
1) 从库统计更新 title、description（与企微卡片「今日概览」一致）；
2) 写入 digest_packs/<token>/manifest.json（与 run_pipeline 摘要包结构一致）。

用法：
  python tools/refresh_pending_digest.py
  python tools/refresh_pending_digest.py --align-latest
      将 digest_date 改为库中「审核通过」记录的最新入库日（与 /api/latest_date 一致），再刷新 manifest 与文案。
"""
from __future__ import annotations

import argparse
import configparser
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from digest_message import (  # noqa: E402
    build_digest_pack_card_url,
    get_digest_payload_for_ingest_date,
    write_manifest_for_existing_token,
)


def _latest_approved_ingest_date(cfg_path: Path) -> str:
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    if not cfg.has_section("database"):
        return ""
    db = cfg["database"]
    import pymysql

    approved_sql = "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = '审核通过'"
    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db.get("charset", "utf8mb4"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT DATE_FORMAT(MAX(d), '%Y-%m-%d') AS d FROM (
                  SELECT DATE(created_at) AS d FROM scraping_infos WHERE {approved_sql}
                  UNION ALL
                  SELECT DATE(updated_at) AS d FROM scraping_infos WHERE {approved_sql}
                ) x WHERE d IS NOT NULL"""
            )
            row = cur.fetchone()
            return str((row or [""])[0] or "").strip()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--align-latest",
        action="store_true",
        help="将 digest_date 改为库中最新入库日（有审核通过数据）",
    )
    args = ap.parse_args()

    cfg_path = ROOT / "config.ini"
    pending_fp = ROOT / "digest_pending_send.json"
    if not pending_fp.is_file():
        print("缺少 digest_pending_send.json", file=sys.stderr)
        sys.exit(1)

    pen = json.loads(pending_fp.read_text(encoding="utf-8"))
    token = (pen.get("token") or "").strip().lower()
    ds = (pen.get("digest_date") or "").strip()
    if not re_token(token):
        print("pending 中 token 无效", file=sys.stderr)
        sys.exit(1)

    if args.align_latest:
        nd = _latest_approved_ingest_date(cfg_path)
        if not re_date(nd):
            print("库中无可用入库日，跳过 align-latest", file=sys.stderr)
            sys.exit(1)
        ds = nd
        pen["digest_date"] = ds

    if not re_date(ds):
        print("digest_date 无效；可用 --align-latest", file=sys.stderr)
        sys.exit(1)

    title, desc, _ = get_digest_payload_for_ingest_date(cfg_path, ds)
    n = write_manifest_for_existing_token(cfg_path, ds, token)

    pen["title"] = title
    pen["description"] = desc
    pen["generated_at"] = datetime.now().replace(microsecond=0).isoformat()

    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    base = ""
    if cfg.has_section("wecom"):
        base = (cfg["wecom"].get("disclose_page_url") or "").strip().rstrip("/")
    if base:
        pen["card_url"] = build_digest_pack_card_url(base, token)
        pen["disclose_page_url"] = base

    pending_fp.write_text(json.dumps(pen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"ok": True, "digest_date": ds, "token": token, "manifest_items": n, "title": title},
            ensure_ascii=False,
        )
    )


def re_token(t: str) -> bool:
    return bool(re.match(r"^[a-f0-9]{32}$", t or ""))


def re_date(s: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s or ""))


if __name__ == "__main__":
    main()
