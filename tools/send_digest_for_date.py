#!/usr/bin/env python3
"""按指定入库日 YYYY-MM-DD 统计、生成摘要包、写入「@售前」预计算表、发给指定企业微信 userid。

H5 上 @售前 依赖表 digest_item_presale_route（Node 在 /api/digest_pack 里 merge）。
生成 manifest 时可用 DIGEST_SKIP_PERSIST_ROUTES 跳过库内写入以加速，本脚本在生成后
再单独调用 persist_digest_item_routes_for_token，与测试环境「跑批后即有售前索引」行为一致。

用法:
  python tools/send_digest_for_date.py 2026-04-30 guandanghai
  python tools/send_digest_for_date.py 2026-04-30 guandanghai --with-ai
  python tools/send_digest_for_date.py 2026-04-30 guandanghai --no-ai
  python tools/send_digest_for_date.py 2026-04-30 guandanghai --no-presale-routes
售前是否用 AI 默认读 config.ini [dispatch] enable_ai_customer_match；--with-ai / --no-ai 仅本次覆盖。

已有摘要包仅补售前索引（不发卡片）:
  python tools/rebuild_digest_item_routes.py <32位token>        # 是否 AI 同 config.ini
  python tools/rebuild_digest_item_routes.py <32位token> --no-ai
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 仅加速 manifest 文件写入；售前路由在本脚本后半段单独 persist
os.environ.setdefault("DIGEST_SKIP_PERSIST_ROUTES", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import configparser
import pymysql

from digest_message import (
    build_digest_pack_card_url,
    get_digest_payload_for_ingest_date,
    materialize_digest_pack,
)
from dispatch_router import load_dispatch_config, persist_digest_item_routes_for_token


def _persist_presale_routes(
    cfg_path: Path, token: str, *, force_ai: bool | None
) -> int:
    mf = ROOT / "digest_packs" / token / "manifest.json"
    if not mf.is_file():
        print("[warn] 无 manifest，跳过售前路由", flush=True)
        return -1
    items = (json.loads(mf.read_text(encoding="utf-8")) or {}).get("items") or []
    dcfg = load_dispatch_config(cfg_path)
    if force_ai is True:
        dcfg.enable_ai_customer_match = True
    elif force_ai is False:
        dcfg.enable_ai_customer_match = False
    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")
    db = cp["database"]
    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        n = persist_digest_item_routes_for_token(
            conn, token, items, dcfg, config_path=cfg_path
        )
        ai_on = dcfg.enable_ai_customer_match
        print(
            f"[presale] digest_item_presale_route 已写入 rows={n}（AI={'开' if ai_on else '关'}，见 config.ini [dispatch] enable_ai_customer_match 或 --with-ai/--no-ai）",
            flush=True,
        )
        return n
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="按入库日发摘要卡片，并写入 @售前 依赖的路由表"
    )
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("userid", help="企业微信 userid")
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--with-ai",
        action="store_true",
        help="本次售前写入强制开 AI（覆盖 config.ini [dispatch] enable_ai_customer_match）",
    )
    g.add_argument(
        "--no-ai",
        action="store_true",
        dest="no_ai",
        help="本次售前写入强制关 AI（覆盖 config.ini）",
    )
    ap.add_argument(
        "--no-presale-routes",
        action="store_true",
        help="不写入 digest_item_presale_route（仅 manifest + 发卡片，与旧行为类似）",
    )
    args = ap.parse_args()

    date = args.date.strip()
    userid = args.userid.strip()
    cfg_path = ROOT / "config.ini"

    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")
    db = cp["database"]
    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db.get("charset", "utf8mb4"),
    )
    approved = (
        "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = '审核通过'"
    )
    ds = de = date
    sql_types = f"""
        SELECT sub_type, COUNT(*) AS c FROM scraping_infos
        WHERE {approved} AND sub_type IN ('招标公告', '中标结果')
        AND (
          (DATE(created_at) >= %s AND DATE(created_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
          OR (DATE(updated_at) >= %s AND DATE(updated_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
        )
        GROUP BY sub_type
    """
    sql_total = f"""
        SELECT COUNT(*) FROM scraping_infos WHERE {approved}
        AND (
          (DATE(created_at) >= %s AND DATE(created_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
          OR (DATE(updated_at) >= %s AND DATE(updated_at) <= DATE_ADD(%s, INTERVAL 1 DAY))
        )
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql_total, (ds, de, ds, de))
            total = cur.fetchone()[0]
            cur.execute(sql_types, (ds, de, ds, de))
            rows = cur.fetchall()
    finally:
        conn.close()

    zbb = zb = 0
    for st, c in rows or []:
        if st == "招标公告":
            zbb = int(c)
        elif st == "中标结果":
            zb = int(c)

    print("=" * 52)
    print(f"  入库日窗口分析（与摘要卡片口径一致）: {date}")
    print(f"  审核通过条数（当日 created/updated）: {total}")
    print(f"  招标公告: {zbb}  |  中标结果: {zb}")
    print("=" * 52)

    title, description, _ = get_digest_payload_for_ingest_date(cfg_path, date)
    token = materialize_digest_pack(cfg_path, date)

    if not args.no_presale_routes:
        force_ai: bool | None = None
        if args.with_ai:
            force_ai = True
        elif getattr(args, "no_ai", False):
            force_ai = False
        _persist_presale_routes(cfg_path, token, force_ai=force_ai)

    base = (cp["wecom"].get("disclose_page_url") or "").strip() or "https://work.weixin.qq.com"
    card_url = build_digest_pack_card_url(base.rstrip("/"), token)

    pending = {
        "token": token,
        "digest_date": date,
        "title": title,
        "description": description,
        "card_url": card_url,
        "disclose_page_url": base.rstrip("/"),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "pipeline_note": f"send_digest_for_date_{date}_{userid}",
    }
    (ROOT / "digest_pending_send.json").write_text(
        json.dumps(pending, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("TITLE:", title)
    print("TOKEN:", token)
    print("CARD_URL:", card_url)

    payload = json.dumps(
        {"touser_list": [userid], "digest_token": token, "auto_route": False},
        ensure_ascii=False,
    )
    r = subprocess.run(
        [sys.executable, str(ROOT / "send_digest_wecom.py")],
        input=payload.encode("utf-8"),
        cwd=str(ROOT),
        capture_output=True,
    )
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    print("--- send_digest_wecom ---")
    print(out)
    if err:
        print(err, file=sys.stderr)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
