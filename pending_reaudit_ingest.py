#!/usr/bin/env python3
"""
待审核 URL 补抓后的即时入库：AI 分析 + 按 detail_url UPDATE scraping_infos（不经过 output JSON 再 ingest）。
供 scraper 在登录后直开详情完成后调用（建议在 asyncio.to_thread 中执行，避免阻塞事件循环）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sync_update_row_from_extract(
    config_file: Path,
    detail_url: str,
    keyword: str,
    sub_type: str,
    title_fb: str,
    article: dict,
    body_content: str,
) -> tuple[bool, str]:
    """
    用刚抓取的详情更新库中同 detail_url 的行。
    返回 (是否更新行数>0, 说明信息)。
    """
    detail_url = (detail_url or "").strip()
    if not detail_url:
        return False, "empty url"

    sys.path.insert(0, str(ROOT))
    import ai_analyze
    import ingest_bank_to_db as ing

    cfg = ing.load_config()
    db_cfg = cfg.get("db")
    if not db_cfg or not db_cfg.get("database"):
        return False, "no database config"

    title = (title_fb or "").strip() or (article.get("title") or "").strip() or "无标题"
    info_type = (sub_type or "采招信息").strip() or "采招信息"
    kw = (keyword or "银行").strip() or "银行"
    crawl_time = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    raw = {
        "title": title,
        "content": body_content or (article.get("content") or ""),
        "url": detail_url,
        "crawl_time": crawl_time,
        "info_type": info_type,
    }

    ai_result = None
    if cfg.get("ai_api_key"):
        try:
            ai_result = ai_analyze.analyze_with_ai(
                raw["title"], raw["content"], config_file
            )
        except Exception as e:
            return False, f"ai error: {e}"

    try:
        row = ai_analyze.build_scraping_info_row(
            raw,
            keyword=kw,
            info_type=info_type,
            ai_result=ai_result,
        )
    except Exception as e:
        return False, f"build_row error: {e}"

    try:
        conn = ing._mysql_connect(db_cfg)
    except Exception as e:
        return False, f"db connect: {e}"

    try:
        n = ing.update_row_by_detail_url(conn, row)
    except Exception as e:
        return False, f"db update: {e}"
    finally:
        conn.close()

    if n <= 0:
        return False, "no row matched detail_url"
    return True, f"updated {n} row(s)"
