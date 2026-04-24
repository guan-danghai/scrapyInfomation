#!/usr/bin/env python3
"""
独立测试：不登录直接访问采招网 URL，或分析本地 HTML 文件中的表格结构。
用于排查表格显示问题。

说明：
- user.bidcenter.com.cn 的链接未登录会跳转到登录页，整页没有 <table> 属正常。
- 公告正文的「表格」可能在：1) 真实 <table>；2) div 等模拟的表格（class 含 table/grid/row/cell）。
- 可传入本地文件：python test_table_analysis.py 本地.html
"""
import asyncio
import re
import sys
from pathlib import Path

# Windows 终端 UTF-8
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TEST_URL = (
    "https://user.bidcenter.com.cn/v2023/#/des/customDesSearch/406645574"
    "?mod=0&tag=1&keywords=%E9%83%91%E5%B7%9E%E9%93%B6%E8%A1%8C%E7%BA%BF%E4%B8%8A%E5%8C%96%E7%A7%91%E5%88%9B%E4%BA%A7%E5%93%81%E9%A1%B9%E7%9B%AE%E4%B8%AD%E6%A0%87%E5%85%AC%E7%A4%BA"
)


def _strip_html_cell(html: str) -> str:
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s, flags=re.I)
    s = " ".join(s.split())
    return s.strip()


def _find_table_bounds(html: str, start: int = 0):
    i = html.find("<table", start)
    if i == -1:
        return None
    depth = 0
    pos = i
    while pos < len(html):
        next_open = html.find("<table", pos)
        next_close = html.find("</table>", pos)
        if next_close == -1:
            return None
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 6
        else:
            depth -= 1
            pos = next_close + 8
            if depth == 0:
                return (i, pos - 1)
    return None


def _table_html_to_markdown(table_html: str) -> str:
    rows = []
    tr_pat = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.DOTALL)
    for tr_m in tr_pat.finditer(table_html):
        tr_inner = tr_m.group(1)
        cells = []
        for tag in ("th", "td"):
            cell_pat = re.compile(rf"<{tag}[^>]*>(.*?)</{tag}>", re.I | re.DOTALL)
            for cell_m in cell_pat.finditer(tr_inner):
                cells.append(_strip_html_cell(cell_m.group(1)))
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    for r in rows:
        while len(r) < ncol:
            r.append("")
    lines = []
    for i, r in enumerate(rows):
        line = "| " + " | ".join(r) + " |"
        lines.append(line)
        if i == 0:
            lines.append("| " + " | ".join(["---"] * ncol) + " |")
    return "\n".join(lines)


def analyze_tables_in_html(html: str, label: str = ""):
    """在 HTML 中查找所有 <table>，打印结构并转 Markdown。"""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.I | re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.I | re.DOTALL)
    tables = []
    pos = 0
    while True:
        bounds = _find_table_bounds(cleaned, pos)
        if bounds is None:
            break
        start, end = bounds
        tables.append(cleaned[start : end + 1])
        pos = end + 1

    print(f"\n{'='*60}")
    print(f"  {label} 共找到 {len(tables)} 个 <table>")
    print("="*60)
    for i, tbl in enumerate(tables, 1):
        print(f"\n--- 表格 {i} (长度 {len(tbl)} 字符) ---")
        snippet = tbl[:800] + ("..." if len(tbl) > 800 else "")
        print("[HTML 片段]")
        print(snippet)
        print("\n[转成 Markdown]")
        md = _table_html_to_markdown(tbl)
        print(md if md else "(未解析到行列)")
        print()
    return tables


def analyze_table_like_divs(html: str, label: str = ""):
    """检测 div 等模拟的表格：class 含 table/grid/row/cell 或 role=grid/row。"""
    # class 含 table、grid、row、cell、tr、td、th 等
    class_pat = re.compile(
        r'<([a-z][a-z0-9]*)\s[^>]*class="[^"]*?(?:table|grid|row|cell|tr|td|th)[^"]*"[^>]*>',
        re.I,
    )
    role_pat = re.compile(r'<([a-z][a-z0-9]*)\s[^>]*role="(?:grid|table|row|rowheader|columnheader|cell)"', re.I)
    hits_class = list(class_pat.finditer(html))
    hits_role = list(role_pat.finditer(html))
    print(f"\n{'='*60}")
    print(f"  {label} 类表格结构（div/span 等）")
    print("="*60)
    if hits_class:
        print(f"  class 含 table|grid|row|cell|tr|td|th 的标签: {len(hits_class)} 处")
        for m in hits_class[:15]:
            tag = m.group(1)
            snippet = html[max(0, m.start() - 20) : m.end() + 80]
            print(f"    标签 <{tag}> 片段: {snippet[:120]}...")
    else:
        print("  class 中未发现 table/grid/row/cell 等。")
    if hits_role:
        print(f"  role=grid|table|row|cell 的标签: {len(hits_role)} 处")
        for m in hits_role[:10]:
            print(f"    片段: {html[m.start():m.end()+60]}...")
    else:
        print("  未发现 role=grid/table/row/cell。")
    # 是否包含「基本信息」等文案（通常和表格在一起）
    if "基本信息" in html or "项目名称" in html:
        print("  正文区含「基本信息」或「项目名称」等关键词（多为表格/键值区）。")
    return hits_class or hits_role


def run_analysis(html: str, source_name: str = "HTML"):
    """对一段 HTML 做表格与类表格结构分析。"""
    analyze_tables_in_html(html, f"{source_name} 中的 <table>")
    analyze_table_like_divs(html, f"{source_name} 中的")
    if "用户登录" in html or "login" in html.lower() and "<table" not in html:
        print("\n[说明] 当前页面疑似登录页，公告正文（含表格）需登录后在详情页查看。")


async def main():
    local_file = (sys.argv[1:] or [None])[0]
    if local_file and Path(local_file).exists():
        # 分析本地 HTML 文件
        path = Path(local_file)
        html = path.read_text(encoding="utf-8", errors="replace")
        print(f"分析本地文件: {path.resolve()}")
        run_analysis(html, path.name)
        return

    from playwright.async_api import async_playwright

    print("启动浏览器（有界面），不登录，直接访问目标 URL...")
    print("URL:", TEST_URL[:80] + "...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=300)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        print("页面已加载，等待 4s 以尽量完成前端渲染。")

        content_selectors = [
            ".article-content", ".detail-content", ".content-body",
            "[class*='detail']", "[class*='article']", "table", "body",
        ]
        full_html = await page.content()
        out_dir = Path(__file__).parent
        raw_path = out_dir / "test_table_analysis_raw.html"
        raw_path.write_text(full_html, encoding="utf-8")
        print(f"\n完整页面 HTML 已保存: {raw_path}")

        run_analysis(full_html, "整页")

        for sel in content_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                inner = await loc.inner_html(timeout=2000)
                if len(inner) < 200:
                    continue
                if "<table" in inner:
                    print(f"\n>>> 选择器 「{sel}」 内包含 table，单独分析：")
                    analyze_tables_in_html(inner, f"选择器 [{sel}]")
                    break
            except Exception:
                continue

        print("\n按回车关闭浏览器...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
