#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性清洗脚本：把数据库 scraping_infos 表中 sub_type 的旧值
按关键词规则重新推断，批量 UPDATE。
执行：python fix_sub_type.py [--dry-run]
  --dry-run  只打印，不写库
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import configparser
import pymysql

DRY_RUN = "--dry-run" in sys.argv

# ── 与 ai_analyze.infer_sub_type_from_title 对齐的关键词规则 ──
KEYWORDS = [
    ("流标",            "流标"),
    ("成交结果",         "成交结果"),
    ("中标候选人公示",    "中标候选人公示"),
    ("候选人公示",       "中标候选人公示"),
    ("中标公示",         "中标公示"),
    ("评标结果公示",      "评标结果公示"),
    ("中标结果公示",      "中标结果公示"),
    ("中标结果",         "中标结果"),
    ("中标公告",         "中标公告"),
    ("中标",            "中标"),
    ("竞争性磋商",       "竞争性磋商"),
    ("磋商",            "磋商"),
    ("竞争性谈判",       "竞争性谈判"),
    ("谈判",            "谈判"),
    ("询价",            "询价"),
    ("邀请招标",         "邀请招标"),
    ("招标公告",         "招标公告"),
    ("招标",            "招标"),
    ("公示",            "公示"),
    ("征集",            "征集"),
    ("邀请",            "邀请"),
    ("采购公告",         "采购公告"),
    ("采购",            "采购"),
]


def infer(title: str) -> str:
    """从标题推断 sub_type。"""
    # 剥离开头 [xxx] 前缀
    clean = re.sub(r"^\[[^\[\]]{1,20}\]", "", title or "").strip() or (title or "").strip()
    for kw, label in KEYWORDS:
        if kw in clean:
            return label
    return "其他"


def load_db_config():
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "config.ini", encoding="utf-8")
    d = cfg["database"]
    return {
        "host": d.get("host", "127.0.0.1"),
        "port": int(d.get("port", 3306)),
        "database": d.get("database", ""),
        "user": d.get("user", "root"),
        "password": d.get("password", ""),
        "charset": d.get("charset", "utf8mb4"),
    }


def main():
    db = load_db_config()
    conn = pymysql.connect(
        host=db["host"], port=db["port"],
        user=db["user"], password=db["password"],
        database=db["database"], charset=db["charset"],
        cursorclass=pymysql.cursors.DictCursor,
    )

    # 1. 先统计现有 sub_type 分布
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sub_type, COUNT(*) AS cnt FROM scraping_infos "
            "GROUP BY sub_type ORDER BY cnt DESC"
        )
        rows = cur.fetchall()

    print("【当前 sub_type 分布】")
    for r in rows:
        print(f"  {str(r['sub_type']):<25}  {r['cnt']} 条")

    # 2. 读所有记录的 id + title
    with conn.cursor() as cur:
        cur.execute("SELECT id, title FROM scraping_infos")
        records = cur.fetchall()

    print(f"\n共 {len(records)} 条记录，开始推断新 sub_type …")

    # 3. 批量推断并 UPDATE
    updated = 0
    unchanged = 0
    from collections import Counter
    new_dist = Counter()

    updates = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, sub_type FROM scraping_infos")
        records = cur.fetchall()

    for rec in records:
        new_type = infer(rec["title"] or "")
        new_dist[new_type] += 1
        if new_type != (rec["sub_type"] or ""):
            updates.append((new_type, rec["id"]))
        else:
            unchanged += 1

    print(f"\n需更新：{len(updates)} 条，无需变更：{unchanged} 条")
    print("\n【推断后 sub_type 分布预览】")
    for t, cnt in sorted(new_dist.items(), key=lambda x: -x[1]):
        print(f"  {t:<25}  {cnt} 条")

    if DRY_RUN:
        print("\n[DRY-RUN] 未写库，加 --dry-run 参数时只预览不执行。去掉参数重新运行即可写库。")
        conn.close()
        return

    # 批量执行
    with conn.cursor() as cur:
        for i, (new_type, rid) in enumerate(updates, 1):
            cur.execute("UPDATE scraping_infos SET sub_type=%s WHERE id=%s", (new_type, rid))
            if i % 200 == 0:
                conn.commit()
                print(f"  已提交 {i}/{len(updates)} …")
    conn.commit()
    conn.close()
    print(f"\n完成！共更新 {len(updates)} 条记录。")


if __name__ == "__main__":
    main()
