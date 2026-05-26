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
    users = set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT project_owner FROM scraping_infos "
                "WHERE DATE(created_at)=CURDATE() AND project_owner IS NOT NULL AND TRIM(project_owner)<>''"
            )
            owners = [str(r.get("project_owner") or "").strip() for r in (cur.fetchall() or [])]
        for owner in owners:
            picked = pick_best_crm_lead_for_owner(conn, owner, dcfg, config_path=ROOT / "config.ini")
            if not picked:
                continue
            for uid in picked.get("userids") or []:
                users.add(uid)
    finally:
        conn.close()
    out = sorted([u for u in users if u])
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
