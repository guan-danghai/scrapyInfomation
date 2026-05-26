#!/usr/bin/env python3
import configparser
import json
from pathlib import Path
import sys

import pymysql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatch_router import _normalize_owner_name


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
    report = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT customer_name, presale
                FROM crm_lead
                WHERE customer_name IN (%s, %s)
                """,
                ("北京银行股份有限公司", "光大理财有限责任公司"),
            )
            rows = cur.fetchall() or []
        report["target_customers_presale"] = rows

        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT project_owner FROM scraping_infos "
                "WHERE DATE(created_at)=CURDATE() AND project_owner IS NOT NULL AND TRIM(project_owner)<>''"
            )
            owners = [str(r.get("project_owner") or "") for r in (cur.fetchall() or [])]
            cur.execute(
                "SELECT DISTINCT customer_name FROM crm_lead "
                "WHERE customer_name IS NOT NULL AND TRIM(customer_name)<>''"
            )
            customers = [str(r.get("customer_name") or "") for r in (cur.fetchall() or [])]

        owner_norm = {_normalize_owner_name(x) for x in owners if _normalize_owner_name(x)}
        customer_norm = {_normalize_owner_name(x) for x in customers if _normalize_owner_name(x)}
        hit = sorted(list(owner_norm & customer_norm))
        miss = sorted(list(owner_norm - customer_norm))
        report["norm_owner_count"] = len(owner_norm)
        report["norm_hit_count"] = len(hit)
        report["norm_hit_rate"] = f"{(len(hit)/len(owner_norm)*100 if owner_norm else 0):.2f}%"
        report["norm_hit_sample"] = hit[:20]
        report["norm_miss_sample"] = miss[:20]
    finally:
        conn.close()
    out = ROOT / "output" / "debug_match_explain.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out))


if __name__ == "__main__":
    main()
