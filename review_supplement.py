#!/usr/bin/env python3
"""
人工补充正文后走 AI 分析并写库：将用户输入作为 detail 正文，更新衍生字段，audit_status=审核通过。
供 web-view-node POST /api/review/supplement 通过 stdin 传入 JSON 调用。

stdin JSON: {"id": 123, "supplement": "用户粘贴的正文..."}
stdout: 单行 JSON {"ok": true} 或 {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        _out(False, f"JSON 无效: {e}")
        sys.exit(1)

    rid = payload.get("id")
    supplement = (payload.get("supplement") or "").strip()
    if not isinstance(rid, int) or rid < 1:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            _out(False, "缺少有效 id")
            sys.exit(1)
    if not supplement:
        _out(False, "补充正文不能为空")
        sys.exit(1)

    sys.path.insert(0, str(ROOT))
    import pymysql

    import ai_analyze
    import ingest_bank_to_db as ing

    cfg = ing.load_config()
    db_cfg = cfg.get("db")
    if not db_cfg or not db_cfg.get("database"):
        _out(False, "未配置 database")
        sys.exit(1)

    conn = ing._mysql_connect(db_cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT keyword, type, sub_type, title, detail_url FROM scraping_infos WHERE id = %s",
                (rid,),
            )
            row0 = cur.fetchone()
    finally:
        conn.close()

    if not row0:
        _out(False, "记录不存在")
        sys.exit(1)

    keyword, typ, sub_type, title, detail_url = row0
    keyword = (keyword or "银行").strip() or "银行"
    info_type = (sub_type or typ or "采招信息").strip() or "采招信息"

    use_ai = bool(cfg.get("ai_api_key"))
    ai_result = None
    if use_ai:
        ai_result = ai_analyze.analyze_with_ai(title or "", supplement, CONFIG_FILE)

    raw_doc = {
        "title": title or "",
        "content": supplement,
        "url": (detail_url or "").strip(),
        "crawl_time": "",
        "info_type": info_type,
    }
    new_row = ai_analyze.build_scraping_info_row(
        raw_doc,
        keyword=keyword,
        info_type=info_type,
        ai_result=ai_result,
    )
    new_row["audit_status"] = ai_analyze.AUDIT_STATUS_APPROVED
    new_row["detail"] = supplement

    conn = ing._mysql_connect(db_cfg)
    try:
        n = ing.update_row_by_id(conn, rid, new_row)
    finally:
        conn.close()

    if not n:
        _out(False, "更新失败（0 行）")
        sys.exit(1)
    _out(True, "")
    sys.exit(0)


def _out(ok: bool, error: str) -> None:
    o = {"ok": ok}
    if not ok and error:
        o["error"] = error
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
