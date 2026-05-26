#!/usr/bin/env python3
"""
按 dispatch_id 计算“当前接收人真实匹配到的记录”，用于摘要页逐条显示 @售前。
stdin JSON: {"dispatch_id":"..."}
stdout 最后一行 JSON:
{
  "ok": true,
  "receiver_userid": "...",
  "receiver_name": "...",
  "matched_record_ids": [1,2,3]
}
"""
from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"
PACK_ROOT = ROOT / "digest_packs"


def _out(obj: dict, code: int) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    sys.exit(code)


def _load_db_cfg() -> dict:
    cp = configparser.ConfigParser()
    cp.read(CONFIG, encoding="utf-8")
    if not cp.has_section("database"):
        raise RuntimeError("config.ini 缺少 [database]")
    d = cp["database"]
    return {
        "host": d.get("host", "127.0.0.1"),
        "port": int(d.get("port", "3306")),
        "user": d.get("user", "root"),
        "password": d.get("password", ""),
        "database": d.get("database", ""),
        "charset": d.get("charset", "utf8mb4"),
    }


def _connect_mysql():
    import pymysql

    db = _load_db_cfg()
    return pymysql.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _resolve_receiver_name(conn, receiver_userid: str, dcfg) -> str:
    if not receiver_userid:
        return ""
    try:
        sql = (
            f"SELECT {dcfg.org_user_name_col} AS n "
            f"FROM {dcfg.org_user_table} "
            f"WHERE {dcfg.org_user_account_col}=%s "
            f"AND ({dcfg.org_user_deleted_col}=0 OR {dcfg.org_user_deleted_col} IS NULL) "
            f"LIMIT 1"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (receiver_userid,))
            row = cur.fetchone() or {}
        return (row.get("n") or "").strip()
    except Exception:
        return ""


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        _out({"ok": False, "error": f"stdin JSON 无效: {e}"}, 1)
    dispatch_id = str((data or {}).get("dispatch_id") or "").strip()
    if not dispatch_id:
        _out({"ok": False, "error": "缺少 dispatch_id"}, 1)

    sys.path.insert(0, str(ROOT))
    from dispatch_router import (
        load_dispatch_config,
        pick_presale_for_digest_item,
    )

    conn = _connect_mysql()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT digest_token, receiver_userid FROM wecom_card_dispatch_log WHERE dispatch_id=%s LIMIT 1",
                (dispatch_id,),
            )
            row = cur.fetchone() or {}
        tok = str(row.get("digest_token") or "").strip().lower()
        receiver = str(row.get("receiver_userid") or "").strip()
        if not tok or len(tok) != 32:
            _out({"ok": False, "error": "分发记录未绑定有效摘要 token"}, 1)
        fp = PACK_ROOT / tok / "manifest.json"
        if not fp.exists():
            _out({"ok": False, "error": f"摘要包不存在: {tok}"}, 1)
        man = json.loads(fp.read_text(encoding="utf-8"))
        items = man.get("items") or []
        dcfg = load_dispatch_config(CONFIG)
        matched_ids: list[int] = []
        for it in items:
            rid = int((it or {}).get("id") or 0)
            if rid < 1:
                continue
            picked = pick_presale_for_digest_item(conn, it, dcfg, config_path=CONFIG)
            uids = list(picked.get("userids") or []) if picked else []
            if receiver and receiver in uids:
                matched_ids.append(rid)
        receiver_name = _resolve_receiver_name(conn, receiver, dcfg)
        _out(
            {
                "ok": True,
                "receiver_userid": receiver,
                "receiver_name": receiver_name,
                "matched_record_ids": sorted(set(matched_ids)),
            },
            0,
        )
    except Exception as e:
        _out({"ok": False, "error": str(e)}, 1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

