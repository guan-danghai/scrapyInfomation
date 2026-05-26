#!/usr/bin/env python3
from pathlib import Path
import configparser
import pymysql
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatch_router import _normalize_owner_name, load_dispatch_config, pick_best_crm_lead_for_owner


def main() -> None:
    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")
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
    ini = ROOT / "config.ini"
    dcfg = load_dispatch_config(ini)

    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM crm_lead")
            lead_cols = [str(r.get("Field") or "").strip() for r in (cur.fetchall() or [])]
        print("CRM_LEAD_COLUMNS", ",".join(lead_cols))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, project_owner, created_at "
                "FROM scraping_infos "
                "WHERE DATE(created_at)=CURDATE() "
                "ORDER BY id DESC"
            )
            rows = cur.fetchall() or []

        total = len(rows)
        owners_with_value = 0
        canonical_hit = 0
        lead_hit_rows = 0
        unique_customers = set()
        matched_customers = set()
        unmatched = []

        for r in rows:
            owner = (r.get("project_owner") or "").strip()
            if owner:
                owners_with_value += 1

            picked = pick_best_crm_lead_for_owner(conn, owner, dcfg, config_path=ini) if owner else None
            customer = (picked.get("canonical_customer") or "").strip() if picked else ""
            uids = list(picked["userids"]) if picked else []
            if customer:
                canonical_hit += 1
                unique_customers.add(customer)
            if uids:
                lead_hit_rows += 1
                if customer:
                    matched_customers.add(customer)
            elif owner:
                unmatched.append(
                    (
                        r.get("id"),
                        owner,
                        customer or "",
                        (r.get("title") or "")[:40],
                    )
                )
            elif not owner:
                unmatched.append((r.get("id"), owner, "", (r.get("title") or "")[:40]))

        print("TOTAL_ROWS", total)
        print("OWNER_NONEMPTY_ROWS", owners_with_value)
        print("CANONICAL_CUSTOMER_ROWS", canonical_hit)
        print("LEAD_MATCHED_ROWS", lead_hit_rows)
        print("LEAD_MATCH_RATE", (f"{(lead_hit_rows / total * 100):.2f}%" if total else "0.00%"))
        print("UNIQUE_CUSTOMERS", len(unique_customers))
        print("MATCHED_UNIQUE_CUSTOMERS", len(matched_customers))
        print("UNMATCHED_SAMPLE_BEGIN")
        for x in unmatched[:20]:
            print(f"ID={x[0]} | owner={x[1]} | canonical={x[2]} | title={x[3]}")
        print("UNMATCHED_SAMPLE_END")

        # 额外统计：按标准化客户名与 crm_lead.customer_name 的潜在命中率
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT customer_name FROM crm_lead "
                "WHERE customer_name IS NOT NULL AND TRIM(customer_name)<>''"
            )
            lead_names = [str(r.get("customer_name") or "") for r in (cur.fetchall() or [])]
        lead_norm = {_normalize_owner_name(x) for x in lead_names if _normalize_owner_name(x)}
        owner_norm = {_normalize_owner_name((r.get("project_owner") or "")) for r in rows if (r.get("project_owner") or "").strip()}
        owner_norm = {x for x in owner_norm if x}
        norm_hit = sorted(list(owner_norm & lead_norm))
        norm_miss = sorted(list(owner_norm - lead_norm))
        print("NORM_CUSTOMER_MATCHED", len(norm_hit))
        print(
            "NORM_CUSTOMER_MATCH_RATE",
            (f"{(len(norm_hit) / len(owner_norm) * 100):.2f}%" if owner_norm else "0.00%"),
        )
        print("NORM_MISS_SAMPLE_BEGIN")
        for x in norm_miss[:20]:
            print(x)
        print("NORM_MISS_SAMPLE_END")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
