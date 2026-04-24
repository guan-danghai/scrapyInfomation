#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对 product_related 为空的记录重新调用 AI 分析，
使用与 ingest_bank_to_db.py 相同的 analyze_with_ai 逻辑。
"""
import sys, configparser, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ai_analyze import analyze_with_ai, infer_product_related_from_text

CONFIG_FILE = ROOT / "config.ini"


def load_db_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    d = cfg["database"]
    return {
        "host": d.get("host"),
        "port": int(d.get("port", 3306)),
        "db": d.get("database"),
        "user": d.get("user"),
        "password": d.get("password"),
        "charset": d.get("charset", "utf8mb4"),
    }


def run():
    import pymysql

    conn = pymysql.connect(
        **{**load_db_config(), "cursorclass": pymysql.cursors.DictCursor}
    )

    try:
        # ── 1. 取出所有 product_related 为空的记录 ─────────────
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, detail "
                "FROM scraping_infos "
                "WHERE product_related IS NULL OR TRIM(product_related)='' "
                "ORDER BY id"
            )
            rows = cur.fetchall()

        total = len(rows)
        print(f"共 {total} 条需要重新分析\n")

        ok_count = err_count = skip_count = 0

        for i, row in enumerate(rows, 1):
            rid   = row["id"]
            title = (row["title"]  or "").strip()
            content = (row["detail"] or "").strip()

            print(f"[{i}/{total}] id={rid}  {title[:60]}")

            # ── 2. 调 AI ────────────────────────────────────────
            ai_result = analyze_with_ai(title, content, CONFIG_FILE)

            if ai_result is None:
                # AI 调用失败 → 规则兜底
                new_val = infer_product_related_from_text(title, content)
                source = "规则兜底"
            else:
                new_val = (ai_result.get("product_related") or "").strip()
                # AI 返回的值校验：只接受合法分类
                if new_val not in ("软件相关", "硬件相关"):
                    # AI 可能返回旧分类词或多选，用规则再判一次
                    new_val = infer_product_related_from_text(title, content)
                    source = "AI返回非法值→规则"
                else:
                    source = "AI"

            if new_val:
                # 同步更新 reserve2（AI 识别到的具体术语）
                terms = ""
                if ai_result and ai_result.get("product_related_terms"):
                    terms = str(ai_result.get("product_related_terms", "")).strip()[:500]

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE scraping_infos "
                        "SET product_related=%s, reserve2=%s "
                        "WHERE id=%s",
                        (new_val, terms or None, rid),
                    )
                conn.commit()
                ok_count += 1
                print(f"         → [{source}] {new_val}  terms={terms[:40] if terms else '-'}")
            else:
                skip_count += 1
                print(f"         → 仍为空（确认非IT）")

            # 避免打爆 API 限速
            if ai_result is not None:
                time.sleep(0.5)

    except Exception as e:
        print(f"\n异常中断：{e}")
        import traceback; traceback.print_exc()
    finally:
        conn.close()

    print(f"\n{'='*50}")
    print(f"完成：已更新 {ok_count} 条 / 仍为空 {skip_count} 条 / 失败 {err_count} 条")


if __name__ == "__main__":
    run()
