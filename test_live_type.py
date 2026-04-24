#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试脚本：真实访问招采网，搜索指定标题关键词，
从页面列表直接读取 info_type，与入库值对比。
用法：python test_live_type.py
"""

import asyncio
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 复用 scraper 中的登录、搜索、解析逻辑
from scraper import (
    login, open_search_tab, click_title_search,
    rand_sleep, USERNAME, PASSWORD, HEADLESS, SLOW_MO,
)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 要测试的5条标题（截取关键词用于搜索） ──────────────────
TEST_CASES = [
    # 采购类
    {"search_kw": "渤海银行机房动力环境监控系统升级改造采购",        "expect_title_contains": "渤海银行",     "note": "DB=采购"},
    {"search_kw": "桂林银行绩效考核管理系统建设项目采购结果",        "expect_title_contains": "桂林银行",     "note": "DB=采购"},
    {"search_kw": "渤海银行新一代票据对接上海票据交易所竞争性采购",  "expect_title_contains": "渤海银行",     "note": "DB=采购公告"},
    # 采招信息类（爬时未拿到标签）
    {"search_kw": "龙江银行第二代货币发行管理系统物流模块维保",      "expect_title_contains": "龙江银行",     "note": "DB=采招信息"},
    {"search_kw": "招商银行上海云数据中心机房基础设施更新UPS",       "expect_title_contains": "招商银行",     "note": "DB=采招信息"},
    # 其他细分类
    {"search_kw": "浙银金融租赁资产管控平台研发采购中标入围",        "expect_title_contains": "浙银金融租赁", "note": "DB=中标"},
    {"search_kw": "雅安市商业银行国内信用证系统竞争性谈判公告",      "expect_title_contains": "雅安市商业银行","note": "DB=竞争性谈判"},
    {"search_kw": "汉口银行个人线上渠道数字化埋点分析平台磋商",      "expect_title_contains": "汉口银行",     "note": "DB=竞争性磋商"},
]


async def fetch_info_type_from_page(context, keyword: str):
    """
    搜索一个关键词，返回列表页前10条的原始 HTML 片段 + 解析到的 info_type。
    直接读取 .ssjg-leixing 和标题文本，两者都打印出来。
    """
    results = []
    search_page = await open_search_tab(context, keyword)
    await click_title_search(search_page)
    await rand_sleep(search_page, 2000, 3500)

    try:
        await search_page.wait_for_selector(".ssjg-list", timeout=12000)
    except PlaywrightTimeout:
        try:
            await search_page.wait_for_selector("text=条信息", timeout=8000)
        except PlaywrightTimeout:
            await search_page.close()
            return results

    await search_page.wait_for_timeout(2000)

    cells = await search_page.locator(".ssjg-list_cell").all()
    for cell in cells[:5]:
        try:
            # ① 尝试取 .ssjg-leixing
            leixing = ""
            try:
                leixing = (await cell.locator(".ssjg-leixing").first.inner_text(timeout=800)).strip()
            except Exception:
                pass

            # ② 取标题文本（a[tid]）
            title_raw = ""
            try:
                title_raw = (await cell.locator("a[tid]").first.inner_text(timeout=1500)).strip()
            except Exception:
                pass

            # ③ 原始行 HTML（前400字）
            raw_html = ""
            try:
                raw_html = (await cell.inner_html(timeout=1500))[:400]
            except Exception:
                pass

            results.append({
                "leixing_dom":  leixing,        # DOM .ssjg-leixing 的值
                "title_raw":    title_raw,       # 标题原始文本（含方括号前缀）
                "raw_html":     raw_html,
            })
        except Exception:
            continue

    await search_page.close()
    return results


async def main():
    print("=" * 70)
    print("  招采网 info_type 真实抓取测试")
    print("=" * 70)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await login(page)
        await page.close()

        for case in TEST_CASES:
            kw = case["search_kw"]
            print(f"\n{'─'*70}")
            print(f"  搜索关键词：{kw}")
            print(f"  预期类型：{case['note']}")
            print(f"{'─'*70}")

            rows = await fetch_info_type_from_page(context, kw)
            if not rows:
                print("  [!] 未搜到任何结果")
                continue

            for i, r in enumerate(rows, 1):
                print(f"  [{i}] .ssjg-leixing DOM = {r['leixing_dom']!r}")
                print(f"  [{i}] 标题原文           = {r['title_raw']!r}")
                print(f"  [{i}] HTML片段           = {r['raw_html'][:200]}")
                print()

        await browser.close()

    print("=" * 70)
    print("  测试完成")
    print("=" * 70)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    asyncio.run(main())
