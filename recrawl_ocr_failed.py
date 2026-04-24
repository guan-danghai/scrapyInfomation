#!/usr/bin/env python3
"""
扫描 output/ 下 JSON 正文中含「OCR 不可用 / RapidOCR 未安装」等标记的条目，
重新登录采招网拉取详情、覆盖原 JSON，并按 detail_url 更新 scraping_infos。

用法（项目根目录）：
  python recrawl_ocr_failed.py              # 扫描整个 output/
  python recrawl_ocr_failed.py --dry-run    # 只列出将处理的 URL/文件，不爬取
  python recrawl_ocr_failed.py --no-db      # 只更新 JSON，不写库（随后可：python sync_json_to_db.py output/日期）

依赖：已配置 config.ini 采招账号、数据库；本机可运行 Playwright；建议已安装 rapidocr-onnxruntime。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CONFIG_FILE = ROOT / "config.ini"

# 与历史落地数据、当前 scraper 提示文案一致
OCR_FAIL_MARKERS = (
    "[OCR 不可用]",
    "RapidOCR 未安装",
    "[说明] 该公告为 PDF 展示",  # 新版：未装 OCR 时的说明
)


def _content_failed(content: str) -> bool:
    c = content or ""
    return any(m in c for m in OCR_FAIL_MARKERS)


def collect_failed_json_by_url(output_root: Path) -> dict[str, list[tuple[Path, str]]]:
    """url -> [(json_path, keyword_dir_name), ...]"""
    by_url: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    if not output_root.is_dir():
        return by_url
    for jpath in output_root.rglob("*.json"):
        if not jpath.is_file():
            continue
        if "汇总" in jpath.name:
            continue
        try:
            raw = json.loads(jpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not _content_failed(raw.get("content") or ""):
            continue
        url = (raw.get("url") or "").strip()
        if not url:
            continue
        keyword = jpath.parent.name
        by_url[url].append((jpath, keyword))
    return dict(by_url)


async def _recrawl_one_url(
    context,
    url: str,
    paths: list[tuple[Path, str]],
) -> tuple[bool, str]:
    """拉取一条 URL，更新所有关联 JSON。返回 (ok, error_message)。"""
    import scraper

    detail = await context.new_page()
    try:
        article = await scraper.extract_article(detail, url)
        body = scraper.extract_body_below_label(article["content"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for jpath, _kw in paths:
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except Exception:
                data = {}
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
            print(f"    [JSON] 已更新 {jpath}")
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        await detail.close()


async def run_recrawl(
    by_url: dict[str, list[tuple[Path, str]]],
    *,
    do_db: bool,
) -> dict[str, Any]:
    import scraper
    from playwright.async_api import async_playwright

    stats = {
        "urls": len(by_url),
        "ok": 0,
        "fail": 0,
        "db_updated": 0,
        "db_skipped": 0,
        "errors": [],
    }

    if not by_url:
        return stats

    ingest_config = None
    conn = None
    if do_db:
        import ingest_bank_to_db as ing
        import ai_analyze

        ingest_config = ing.load_config()
        db_cfg = ingest_config.get("db")
        if not db_cfg or not db_cfg.get("database"):
            print("[WARN] 未配置数据库，跳过写库（等价 --no-db）")
            do_db = False
        else:
            conn = ing._mysql_connect(db_cfg)

    use_ai = bool((ingest_config or {}).get("ai_api_key")) if ingest_config else False

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

        for url, plist in by_url.items():
            title_hint = plist[0][0].name[:50]
            print(f"\n  [重爬] {url}\n        文件: {len(plist)} 个 | {title_hint}...")
            ok, err = await _recrawl_one_url(context, url, plist)
            if not ok:
                stats["fail"] += 1
                stats["errors"].append((url, err))
                print(f"    [FAIL] {err}")
                await asyncio.sleep(1)
                continue
            stats["ok"] += 1

            if do_db and conn:
                import ingest_bank_to_db as ing
                import ai_analyze

                jpath = plist[0][0]
                keyword = plist[0][1]
                raw = json.loads(jpath.read_text(encoding="utf-8"))
                title = raw.get("title") or ""
                content = raw.get("content") or ""
                ai_result = None
                if use_ai:
                    ai_result = ai_analyze.analyze_with_ai(
                        title, content, CONFIG_FILE
                    )
                info_type = (raw.get("info_type") or "采招信息").strip()
                row = ai_analyze.build_scraping_info_row(
                    raw,
                    keyword=keyword,
                    info_type=info_type,
                    ai_result=ai_result,
                )
                n = ing.update_row_by_detail_url(conn, row)
                if n:
                    stats["db_updated"] += 1
                    print(f"    [DB] 已按 detail_url 更新 {n} 行")
                else:
                    stats["db_skipped"] += 1
                    print(f"    [DB] 无匹配 detail_url，未更新（可能未入库过）")

            await asyncio.sleep(1.5)

        await browser.close()

    if conn:
        conn.close()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="重爬 OCR 失败条目并更新 JSON/数据库")
    parser.add_argument(
        "--output-root",
        type=str,
        default="output",
        help="输出根目录，默认 output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将处理的 URL 与文件数，不启动浏览器",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="只写 JSON，不更新 MySQL",
    )
    args = parser.parse_args()

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    out = (ROOT / args.output_root).resolve()
    by_url = collect_failed_json_by_url(out)

    print(f"扫描目录: {out}")
    print(f"待处理 URL 数: {len(by_url)}（按 URL 去重后逐条重爬）")
    for u, plist in list(by_url.items())[:20]:
        print(f"  - {u}  ({len(plist)} 个 JSON)")
    if len(by_url) > 20:
        print(f"  ... 另有 {len(by_url) - 20} 个 URL")

    if args.dry_run:
        return

    if not by_url:
        print("没有需要重爬的 JSON。")
        return

    stats = asyncio.run(
        run_recrawl(by_url, do_db=not args.no_db)
    )
    print(
        f"\n完成: URL 成功 {stats['ok']}, 失败 {stats['fail']}, "
        f"库更新 {stats['db_updated']}, 库未匹配 {stats['db_skipped']}"
    )


if __name__ == "__main__":
    main()
