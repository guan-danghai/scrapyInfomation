#!/usr/bin/env python3
import configparser
import json
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatch_router import load_dispatch_config, pick_best_crm_lead_for_owner


def main() -> None:
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "config.ini", encoding="utf-8")
    db = cfg["database"]
    conn = pymysql.connect(
        host=db.get("host"),
        port=int(db.get("port")),
        user=db.get("user"),
        password=db.get("password"),
        database=db.get("database"),
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
    )
    dcfg = load_dispatch_config(ROOT / "config.ini")
    matched = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, project_owner FROM scraping_infos "
                "WHERE DATE(created_at)=CURDATE() ORDER BY id DESC"
            )
            rows = cur.fetchall() or []
        for r in rows:
            owner = (r.get("project_owner") or "").strip()
            if not owner:
                continue
            picked = pick_best_crm_lead_for_owner(conn, owner, dcfg, config_path=ROOT / "config.ini")
            uids = list(picked["userids"]) if picked else []
            if uids:
                matched.append(
                    {
                        "id": r.get("id"),
                        "title": (r.get("title") or "").strip(),
                        "project_owner": owner,
                        "matched_customer": (picked or {}).get("canonical_customer") or "",
                        "to_userids": uids,
                    }
                )
    finally:
        conn.close()

    out = {
        "matched_count": len(matched),
        "items": matched,
    }
    out_fp = ROOT / "output" / "today_matched_rows.json"
    out_fp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_fp))


if __name__ == "__main__":
    main()
