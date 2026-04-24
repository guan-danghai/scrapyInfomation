#!/usr/bin/env python3
"""
将指定目录（含子目录）下的爬取 JSON 按 detail_url 更新已入库的 scraping_infos。
用于「已手工或 recrawl_ocr_failed 改过 JSON」后补写数据库，不会插入新 URL。

用法：
  python sync_json_to_db.py output/2026-04-07
  python sync_json_to_db.py output/2026-04-07/农村信用
  python sync_json_to_db.py output/2026-04-20/银行/a.json output/2026-04-20/银行/b.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CONFIG_FILE = ROOT / "config.ini"


def _collect_json_paths(argv_paths: list[str]) -> list[Path]:
    """参数可为若干 .json 文件路径，或目录（递归 *.json）；去重并保持顺序。"""
    out: list[Path] = []
    for a in argv_paths:
        p = (ROOT / a).resolve()
        if not p.exists():
            raise FileNotFoundError(str(p))
        if p.is_file():
            if p.suffix.lower() != ".json":
                raise ValueError(f"不是 JSON 文件: {p}")
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.rglob("*.json")))
        else:
            raise ValueError(f"既不是文件也不是目录: {p}")
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "用法: python sync_json_to_db.py <目录> [更多目录…]\n"
            "      python sync_json_to_db.py <文件.json> [更多.json…]"
        )
        sys.exit(1)

    try:
        json_paths = _collect_json_paths(sys.argv[1:])
    except (FileNotFoundError, ValueError) as e:
        print(e)
        sys.exit(1)

    import ai_analyze
    import ingest_bank_to_db as ing

    cfg = ing.load_config()
    db_cfg = cfg.get("db")
    if not db_cfg or not db_cfg.get("database"):
        print("请配置 config.ini [database]")
        sys.exit(1)
    use_ai = bool(cfg.get("ai_api_key"))
    conn = ing._mysql_connect(db_cfg)

    updated = 0
    skipped = 0
    errors = 0
    for jpath in sorted(json_paths):
        if "汇总" in jpath.name:
            continue
        try:
            raw = json.loads(jpath.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[SKIP] 读取失败 {jpath.name}: {e}")
            errors += 1
            continue
        url = (raw.get("url") or "").strip()
        if not url:
            continue
        if not ing.exists_by_detail_url(conn, url):
            skipped += 1
            continue
        keyword = jpath.parent.name
        title = raw.get("title") or ""
        content = raw.get("content") or ""
        ai_result = None
        if use_ai:
            ai_result = ai_analyze.analyze_with_ai(title, content, CONFIG_FILE)
        info_type = (raw.get("info_type") or "采招信息").strip()
        row = ai_analyze.build_scraping_info_row(
            raw,
            keyword=keyword,
            info_type=info_type,
            ai_result=ai_result,
        )
        n = ing.update_row_by_detail_url(conn, row)
        if n:
            updated += 1
            print(f"[OK] 更新 {n} 行 | {title[:60]}...")
        else:
            skipped += 1
    conn.close()
    print(f"\n完成: 更新 {updated} 条, 跳过(库中无 URL) {skipped} 个文件, 错误 {errors}")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
