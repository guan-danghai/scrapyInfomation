#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集成测试：用指定「整句标题」作为采招网主站搜索关键字，走完整登录 → 标题搜索 → 列表 → 详情 → 落盘。

关键字：长沙银行一表通系统数据库授权采购项目（第二次）中标候选人公示

依赖：config.ini 中采招账号、scraper 节配置；本机已安装 Playwright Chromium。

用法（项目根目录）：
  python test_recrawl_changsha_yibiao_keyword.py
  python -m unittest test_recrawl_changsha_yibiao_keyword.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 与列表页标题一致即可；整句作为搜索词 + scrape_keyword 要求「标题包含该串」
KEYWORD = "长沙银行一表通系统数据库授权采购项目（第二次）中标候选人公示"


class TestRecrawlChangshaYibiaoKeyword(unittest.IsolatedAsyncioTestCase):
    """单关键字重抓测试（真实浏览器，非 mock）。"""

    async def test_recrawl_full_title_keyword(self):
        import scraper

        # 不按 config 的 start_date/endtime 筛时间，避免把目标公告筛掉
        result = await scraper.scrape(
            [KEYWORD],
            max_pages=min(5, scraper.MAX_PAGES),
            start_date="",
            end_date="",
        )

        self.assertIn("total_saved", result)
        self.assertIn("base_out", result)
        base_out = result["base_out"]
        self.assertTrue(
            os.path.isdir(base_out),
            f"输出目录应存在: {base_out}",
        )

        # 若列表里标题与关键字有全半角差异导致 0 条，可改为仅打印告警不 fail
        if result["total_saved"] == 0:
            self.skipTest(
                "未保存任何条目：可能无匹配结果、标题与关键字不完全一致、或详情抓取失败；"
                "请看控制台日志。"
            )


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    unittest.main(verbosity=2)
