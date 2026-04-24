#!/usr/bin/env python3
"""
按某条 output JSON 中的 url 重新登录采招网、抓详情、覆盖该 JSON，并可选按 detail_url 更新库。
用法（项目根目录）：
  python recrawl_one_json.py "output/2026-04-10/银行/xxx.json"
  python recrawl_one_json.py "output/..." --no-db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"


async def _run(jpath: Path, *, do_db: bool) -> int:
    import scraper
    from playwright.async_api import async_playwright

    raw = json.loads(jpath.read_text(encoding="utf-8"))
    url = (raw.get("url") or "").strip()
    if not url:
        print("JSON 缺少 url")
        return 1

    keyword = jpath.parent.name
    paths = [(jpath, keyword)]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=scraper.HEADLESS,
            slow_mo=scraper.SLOW_MO,
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        main_page = await context.new_page()
        try:
            await scraper.login(main_page)
        finally:
            await main_page.close()

        detail = await context.new_page()
        try:
            article = await scraper.extract_article(detail, url)
            body = scraper.extract_body_below_label(article["content"])
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data = json.loads(jpath.read_text(encoding="utf-8"))
            data["content"] = body
            data["text_blocks"] = article.get("text_blocks") or []
            data["tables"] = article.get("tables") or []
            data["crawl_time"] = now
            if article.get("title"):
                data["title"] = article["title"]
            jpath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[JSON] 已更新 {jpath}")
            print(f"[正文预览前 500 字]\n{body[:500]!r}")
        finally:
            await detail.close()

        await browser.close()

    if do_db:
        import ingest_bank_to_db as ing
        import ai_analyze

        ingest_config = ing.load_config()
        db_cfg = ingest_config.get("db")
        if not db_cfg or not db_cfg.get("database"):
            print("[WARN] 未配置数据库，跳过写库")
            return 0
        conn = ing._mysql_connect(db_cfg)
        try:
            raw2 = json.loads(jpath.read_text(encoding="utf-8"))
            title = raw2.get("title") or ""
            content = raw2.get("content") or ""
            use_ai = bool(ingest_config.get("ai_api_key"))
            ai_result = (
                ai_analyze.analyze_with_ai(title, content, CONFIG_FILE)
                if use_ai
                else None
            )
            info_type = (raw2.get("info_type") or "采招信息").strip()
            row = ai_analyze.build_scraping_info_row(
                raw2,
                keyword=keyword,
                info_type=info_type,
                ai_result=ai_result,
            )
            n = ing.update_row_by_detail_url(conn, row)
            print(f"[DB] update_row_by_detail_url 影响行数: {n}")
        finally:
            conn.close()

    return 0


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="按 JSON 内 url 重爬单条并覆盖文件")
    ap.add_argument("json_path", type=str, help="output 下某条 .json 路径")
    ap.add_argument("--no-db", action="store_true", help="不写 MySQL")
    args = ap.parse_args()
    jpath = (ROOT / args.json_path).resolve()
    if not jpath.is_file():
        print(f"文件不存在: {jpath}")
        sys.exit(1)
    code = asyncio.run(_run(jpath, do_db=not args.no_db))
    sys.exit(code)


if __name__ == "__main__":
    main()
