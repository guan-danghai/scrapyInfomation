#!/usr/bin/env python3
"""
中国采招网 信息爬取工具
流程：登录 → 主站搜索框输入关键词 → jq_search() 打开新标签页
     → 点击"标题搜索" → 轮询搜索结果 → 逐条打开详情 → 保存 txt
"""

import asyncio
import functools
import random
import re
import os
import sys
import io
import time
import uuid
import configparser
from pathlib import Path
from typing import Optional

try:
    import wecom_notify
except ImportError:
    wecom_notify = None
try:
    from ai_analyze import is_tech_related_by_title
except ImportError:
    is_tech_related_by_title = None
from datetime import datetime
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse, unquote, urljoin
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import pdfplumber

# Windows 终端默认 GBK，强制 UTF-8
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _is_valid_chinese(s: str) -> bool:
    """判断字符串是否含有合法中文字符"""
    return any('\u4e00' <= c <= '\u9fff' for c in s)


def _decode_mojibake(s: str) -> str:
    """
    Windows Shell 传入中文参数时常见乱码场景修复。
    依次尝试多种编码组合，取第一个能还原出中文的结果。
    """
    for enc_from, enc_to in [("gbk", "utf-8"), ("utf-8", "gbk"), ("latin-1", "utf-8")]:
        try:
            fixed = s.encode(enc_from).decode(enc_to)
            if _is_valid_chinese(fixed):
                return fixed
        except Exception:
            continue
    return s


def unwrap_dom_double_glyphs(text: str, min_len: int = 8) -> str:
    """
    部分页面把每个字符在 DOM 里写两遍（反爬/双份可见层），inner_text 会得到「福福建建省省…」。
    对每一行：若去掉首尾空白后长度≥min_len、长度为偶数、且两两相同，则折叠为单字序列。
    """
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if (
            len(s) >= min_len
            and len(s) % 2 == 0
            and all(s[i] == s[i + 1] for i in range(0, len(s), 2))
        ):
            collapsed = "".join(s[i] for i in range(0, len(s), 2))
            left_ws = line[: len(line) - len(line.lstrip())]
            right_ws = line[len(line.rstrip()) :]
            out.append(left_ws + collapsed + right_ws)
        else:
            out.append(line)
    return "\n".join(out)


async def rand_sleep(page, lo: int = 1000, hi: int = 4000) -> None:
    """随机等待 [lo, hi] 毫秒，模拟真人操作节奏，对抗反爬检测。
    page 为 None 时降级为 asyncio.sleep。"""
    ms = random.randint(lo, hi)
    if page is not None:
        try:
            await page.wait_for_timeout(ms)
            return
        except Exception:
            pass
    await asyncio.sleep(ms / 1000)


def fix_argv_encoding() -> list[str]:
    """
    Windows PowerShell 在 GBK 代码页下传递 UTF-8 中文参数时会产生乱码。
    尝试多种策略还原；若参数本身已含正确中文则不处理。

    注意：Shell 层乱码有时无法完全修复，建议将中文关键词写入 config.ini，
    直接运行 python scraper.py（不传参数）以规避此问题。
    """
    if sys.platform != "win32":
        return sys.argv[1:]
    result = []
    for arg in sys.argv[1:]:
        if _is_valid_chinese(arg):
            result.append(arg)
        else:
            fixed = _decode_mojibake(arg)
            if fixed != arg:
                print(f"  [编码修复] {arg!r} → {fixed!r}")
            result.append(fixed)
    return result

# ======================== 读取配置文件 ========================
_CONFIG_FILE = Path(__file__).parent / "config.ini"

def _normalize_output_format(v: str) -> str:
    o = (v or "txt").strip().lower()
    return o if o in ("txt", "html", "json", "both") else "txt"


def _resolve_date(v: str) -> str:
    """若配置为 today（不区分大小写），则返回当天日期 YYYY-MM-DD，否则返回原值。用于每日跑批。"""
    if not v or not (v := v.strip()):
        return ""
    if v.lower() == "today":
        return datetime.now().strftime("%Y-%m-%d")
    return v


def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding="utf-8")
    s = cfg["scraper"]
    raw_kws  = re.split(r"[,，]", s.get("keywords", ""))
    keywords = [kw.strip() for kw in raw_kws if kw.strip()]
    raw_excl = re.split(r"[,，]", s.get("exclude_keywords", ""))
    exclude_keywords = [kw.strip() for kw in raw_excl if kw.strip()]
    out = {
        "username":         s.get("username", ""),
        "password":         s.get("password", ""),
        "keywords":         keywords,
        "max_pages":        s.getint("max_pages", 3),
        "headless":         s.getboolean("headless", False),
        "output_dir":       s.get("output_dir", "output"),
        "output_format":    _normalize_output_format(s.get("output_format", "txt")),
        "slow_mo":          s.getint("slow_mo", 400),
        "start_date":       _resolve_date(s.get("start_date", "")),
        "end_date":         _resolve_date(s.get("end_date", "")),
        "exclude_keywords": exclude_keywords,
        "search_mode":      s.getint("search_mode", 0),
        # 公告正文内 <img> 截图/下载后 OCR（易混入侧栏或错字导致正文混乱；false=关闭）
        "article_inline_image_ocr": s.getboolean(
            "article_inline_image_ocr", False
        ),
    }
    if cfg.has_section("wecom"):
        w = cfg["wecom"]
        out["wecom_enabled"] = w.getboolean("enabled", False)
        out["wecom_webhook_url"] = w.get("webhook_url", "").strip()
        out["wecom_corp_id"] = w.get("corp_id", "").strip()
        try:
            out["wecom_agent_id"] = w.getint("agent_id", 0) or None
        except (ValueError, TypeError):
            out["wecom_agent_id"] = None
        out["wecom_secret"] = w.get("secret", "").strip()
        out["wecom_to_user"] = w.get("to_user", "").strip() or None
        out["wecom_to_chatid"] = w.get("to_chatid", "").strip() or None
    else:
        out["wecom_enabled"] = False
        out["wecom_webhook_url"] = ""
        out["wecom_corp_id"] = ""
        out["wecom_agent_id"] = None
        out["wecom_secret"] = ""
        out["wecom_to_user"] = None
        out["wecom_to_chatid"] = None
    return out

_CFG       = load_config()
LOGIN_URL  = "https://sso.bidcenter.com.cn/login/"
HOME_URL   = "https://www.bidcenter.com.cn"
USERNAME   = _CFG["username"]
PASSWORD   = _CFG["password"]
OUTPUT_DIR   = _CFG["output_dir"]
OUTPUT_FORMAT = _CFG["output_format"]
HEADLESS    = _CFG["headless"]
MAX_PAGES   = _CFG["max_pages"]
SLOW_MO     = _CFG["slow_mo"]
START_DATE  = _CFG["start_date"]
END_DATE    = _CFG["end_date"]
EXCLUDE_KEYWORDS = _CFG["exclude_keywords"]
SEARCH_MODE      = _CFG["search_mode"]
ARTICLE_INLINE_IMAGE_OCR = bool(_CFG.get("article_inline_image_ocr", False))
# ============================================================


# ─────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", name)
    return name.strip().strip("_")[:120]


def _strip_html_cell(html: str) -> str:
    """去掉单元格 HTML 标签，保留文本，合并空白。"""
    if not html:
        return ""
    # 先换行符统一，再去标签
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s, flags=re.I)
    s = " ".join(s.split())
    return s.strip()


def _find_table_bounds(html: str, start: int = 0):
    """从 start 起找下一个 <table>...</table> 的 (start, end) 闭区间，end 指向 '</table>' 最后一个字符。"""
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
    """将一块 table 的 HTML 转成 Markdown 表格（无表头分隔行时自动加）。"""
    rows = []
    # 找所有 <tr>...</tr>（非贪婪，避免跨多行出问题则用 DOTALL）
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
    # 统一列数（按第一行）
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


def _table_html_to_list(table_html: str) -> list:
    """将一块 table HTML 解析为二维列表 list[list[str]]，供 JSON 输出使用。"""
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
    # 统一列数
    if rows:
        ncol = max(len(r) for r in rows)
        for r in rows:
            while len(r) < ncol:
                r.append("")
    return rows


def html_to_structured_blocks(html: str) -> dict:
    """
    将正文 HTML 同时解析为：
    - text_blocks: list[str]  段落纯文本列表（去掉表格部分）
    - tables:      list[list[list[str]]]  所有表格的二维列表
    供 JSON 保存使用，保留完整结构，方便后续 AI 分析入库。
    """
    if not html or not html.strip():
        return {"text_blocks": [], "tables": []}

    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.I | re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.I | re.DOTALL)

    text_blocks = []
    tables = []
    pos = 0

    while True:
        bounds = _find_table_bounds(cleaned, pos)
        if bounds is None:
            break
        start, end = bounds
        # 表格前的段落文本
        before = cleaned[pos:start]
        before_text = re.sub(r"<[^>]+>", " ", before)
        before_text = re.sub(r"&nbsp;", " ", before_text, flags=re.I)
        before_text = " ".join(before_text.split()).strip()
        if before_text:
            text_blocks.append(before_text)
        # 表格解析为二维列表
        tbl = _table_html_to_list(cleaned[start: end + 1])
        if tbl:
            tables.append(tbl)
        pos = end + 1

    # 剩余段落文本
    rest = cleaned[pos:]
    rest_text = re.sub(r"<[^>]+>", " ", rest)
    rest_text = re.sub(r"&nbsp;", " ", rest_text, flags=re.I)
    rest_text = " ".join(rest_text.split()).strip()
    if rest_text:
        text_blocks.append(rest_text)

    return {"text_blocks": text_blocks, "tables": tables}


def html_content_to_text_with_tables(html: str) -> str:
    """
    将正文 HTML 转为纯文本，其中 <table> 转为 Markdown 表格，便于在 .txt 中保留表格形式。
    不引入额外依赖，仅用正则与字符串处理。
    """
    if not html or not html.strip():
        return ""
    # 去掉 script/style 内容，避免其中的 </table> 干扰
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.I | re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.I | re.DOTALL)
    parts = []
    pos = 0
    while True:
        bounds = _find_table_bounds(cleaned, pos)
        if bounds is None:
            break
        start, end = bounds
        # 表格前的文本
        before = cleaned[pos:start]
        before_text = re.sub(r"<[^>]+>", " ", before)
        before_text = re.sub(r"&nbsp;", " ", before_text, flags=re.I)
        before_text = " ".join(before_text.split()).strip()
        if before_text:
            parts.append(before_text)
        parts.append(_table_html_to_markdown(cleaned[start : end + 1]))
        pos = end + 1
    # 剩余部分
    rest = cleaned[pos:]
    rest_text = re.sub(r"<[^>]+>", " ", rest)
    rest_text = re.sub(r"&nbsp;", " ", rest_text, flags=re.I)
    rest_text = " ".join(rest_text.split()).strip()
    if rest_text:
        parts.append(rest_text)
    return "\n\n".join(p for p in parts if p).strip()


# 招商银行等：正文区用 div 排版，inner_text 变成「表头逐行 + 数据逐行」，无 <table>。
# 下列表头按顺序竖排出现时，合并为制表符行，与 detail.html 中「\\t 分隔自动转表格」一致。
_VERTICAL_TABLE_HEADER_SETS = (
    ("标段名称", "分类", "报价项名称", "规格参数", "计量单位", "数量"),
)


def _next_meaningful_line_idx(j: int, lines: list) -> int:
    """跳过空行、仅空白、仅制表符的行（采招网 div 表格 inner_text 常在格间插入 \\n\\n\\t\\n\\n）。"""
    while j < len(lines):
        if lines[j].replace("\t", "").strip():
            return j
        j += 1
    return len(lines)


def _existing_table_has_header_row(
    existing_tables: list, headers: tuple[str, ...]
) -> bool:
    """已有 DOM 解析表格且首行与已知表头一致时，不再做竖排还原。"""
    want = list(headers)
    for t in existing_tables or []:
        if not t or len(t) < 1:
            continue
        r0 = [str(c or "").strip() for c in t[0]]
        if len(r0) >= len(want) and r0[: len(want)] == want:
            return True
    return False


def repair_vertical_list_tables_in_plaintext(
    content: str, existing_tables: Optional[list] = None,
) -> tuple[str, list]:
    """
    将「二、采购内容」下竖排的表头+数据还原为两行制表符文本，并返回可写入 JSON 的表格行。
    不改变已成功解析的 <table> 结果（由 existing_tables 与表头比对）。
    返回 (新正文, [[表头行], [数据行], ...] 的列表)。
    """
    if not content or not isinstance(content, str):
        return content, []
    merged_existing: list = list(existing_tables or [])
    out_tables: list = []
    work = content
    max_passes = 5
    for _ in range(max_passes):
        lines = work.splitlines()
        replaced = False
        for i, raw in enumerate(lines):
            ln = raw.strip()
            if not ln or not re.match(r"^二[、,，．.]\s*采购内容", ln):
                continue
            j = _next_meaningful_line_idx(i + 1, lines)
            for headers in _VERTICAL_TABLE_HEADER_SETS:
                if _existing_table_has_header_row(merged_existing, headers):
                    continue
                n = len(headers)
                jj = j
                ok = True
                header_start_idx = jj
                for k in range(n):
                    if jj >= len(lines):
                        ok = False
                        break
                    cell = lines[jj].strip()
                    exp = headers[k]
                    if cell != exp and not cell.startswith(exp):
                        ok = False
                        break
                    jj += 1
                    jj = _next_meaningful_line_idx(jj, lines)
                if not ok:
                    continue
                vals: list[str] = []
                val_start_idx = jj
                for k in range(n):
                    if jj >= len(lines):
                        ok = False
                        break
                    v = lines[jj].strip()
                    if not v:
                        ok = False
                        break
                    if re.match(r"^[三四五六七八九十]+[、,，．.]", v):
                        ok = False
                        break
                    vals.append(v)
                    last_v_idx = jj
                    jj += 1
                    jj = _next_meaningful_line_idx(jj, lines)
                if not ok or len(vals) != n:
                    continue
                new_lines = (
                    lines[:header_start_idx]
                    + ["\t".join(headers), "\t".join(vals)]
                    + lines[last_v_idx + 1 :]
                )
                work = "\n".join(new_lines)
                one_tbl = [list(headers), vals]
                out_tables.append(one_tbl)
                merged_existing.append(one_tbl)
                replaced = True
                print("    [正文] 竖排「采购内容」栏目已还原为表格（制表符行）")
                break
            if replaced:
                break
        if not replaced:
            break
    return work, out_tables


def _existing_has_seq_bidder_price_row(existing_tables: list) -> bool:
    """已有「序号 + 中标单位 + 金额」类表格时不再做 PDF 压平还原。"""
    for t in existing_tables or []:
        if not t or len(t) < 1:
            continue
        r0 = [str(c or "").strip() for c in t[0]]
        if len(r0) >= 2 and r0[0] == "序号" and "中标" in r0[1]:
            return True
    return False


def repair_weihai_style_review_result_table(
    content: str, existing_tables: Optional[list] = None,
) -> tuple[str, list]:
    """
    威海银行等：PDF/inner_text 将「四、评审结果」下表格压成多行，例如：
      投标报价 / 序号 中标单位名称 / （人民币元，含税） / 1 公司名 金额
    还原为两行制表符 + tables。
    """
    if not content or not isinstance(content, str):
        return content, []
    if _existing_has_seq_bidder_price_row(existing_tables):
        return content, []
    lines = content.splitlines()
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if not ln or not re.match(r"^四[、,，．.]\s*评审结果", ln):
            continue
        block_start = _next_meaningful_line_idx(i + 1, lines)
        if block_start >= len(lines):
            continue
        j_scan = block_start
        if lines[j_scan].strip() == "投标报价":
            j_scan = _next_meaningful_line_idx(j_scan + 1, lines)
        if j_scan >= len(lines):
            continue
        if not re.match(r"^序号\s+中标单位名称\s*$", lines[j_scan].strip()):
            continue
        j2 = _next_meaningful_line_idx(j_scan + 1, lines)
        if j2 >= len(lines):
            continue
        cap = lines[j2].strip()
        if not (
            (cap.startswith("（") or cap.startswith("("))
            and ("含税" in cap or "元" in cap)
        ):
            continue
        j3 = _next_meaningful_line_idx(j2 + 1, lines)
        if j3 >= len(lines):
            continue
        parts = lines[j3].strip().split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        amount_raw = parts[-1].replace(",", "")
        if not re.match(r"^[\d.]+$", amount_raw):
            continue
        seq = parts[0]
        company = " ".join(parts[1:-1]).strip()
        if not company:
            continue
        headers = ["序号", "中标单位名称", "投标报价（人民币元，含税）"]
        vals = [seq, company, parts[-1]]
        new_lines = (
            lines[:block_start]
            + ["\t".join(headers), "\t".join(vals)]
            + lines[j3 + 1 :]
        )
        work = "\n".join(new_lines)
        tbl = [headers, vals]
        print("    [正文] 「四、评审结果」压平表格已还原（PDF/多行版式）")
        return work, [tbl]
    return content, []


def _existing_has_seq_supplier_two_col(existing_tables: list) -> bool:
    """已有「序号 + 中标供应商/单位」两列表头时不再还原。"""
    for t in existing_tables or []:
        if not t or not t[0] or len(t[0]) < 2:
            continue
        r0 = [str(c or "").strip() for c in t[0][:2]]
        if r0[0] != "序号":
            continue
        if "供应商" in r0[1] or r0[1] == "中标单位名称":
            return True
    return False


def repair_zhongbiao_result_vertical_supplier_table(
    content: str, existing_tables: Optional[list] = None,
) -> tuple[str, list]:
    """
    温州银行等：「七、中标结果」等小节下竖排「序号」「中标供应商名称」+ 序号值 + 供应商名（div/PDF 压平）。
    与威海「四、评审结果」三列规则互斥小节标题，不改变既有匹配。
    """
    if not content or not isinstance(content, str):
        return content, []
    if _existing_has_seq_supplier_two_col(existing_tables):
        return content, []
    lines = content.splitlines()
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if not ln or not re.match(
            r"^[一二三四五六七八九十]+[、,，．.]\s*中标结果", ln
        ):
            continue
        j = _next_meaningful_line_idx(i + 1, lines)
        if j >= len(lines) or lines[j].strip() != "序号":
            continue
        j2 = _next_meaningful_line_idx(j + 1, lines)
        if j2 >= len(lines):
            continue
        h2 = lines[j2].strip()
        if h2 not in ("中标供应商名称", "中标单位名称"):
            continue
        j3 = _next_meaningful_line_idx(j2 + 1, lines)
        if j3 >= len(lines):
            continue
        seq_cell = lines[j3].strip()
        if not seq_cell.isdigit():
            continue
        j4 = _next_meaningful_line_idx(j3 + 1, lines)
        if j4 >= len(lines):
            continue
        company = lines[j4].strip()
        if not company:
            continue
        headers = ["序号", h2]
        vals = [seq_cell, company]
        new_lines = (
            lines[:j]
            + ["\t".join(headers), "\t".join(vals)]
            + lines[j4 + 1 :]
        )
        work = "\n".join(new_lines)
        tbl = [headers, vals]
        print("    [正文] 「中标结果」竖排两列表格已还原")
        return work, [tbl]
    return content, []


_CHENGJIAO_CONTENT_HEADERS = (
    "序号",
    "采购内容",
    "数量",
    "成交供应商",
)


def _existing_has_chengjiao_four_col(existing_tables: list) -> bool:
    want = list(_CHENGJIAO_CONTENT_HEADERS)
    for t in existing_tables or []:
        if not t or not t[0] or len(t[0]) < 4:
            continue
        r0 = [str(c or "").strip() for c in t[0][:4]]
        if r0 == want:
            return True
    return False


def repair_chengjiao_content_vertical_four_col_table(
    content: str, existing_tables: Optional[list] = None,
) -> tuple[str, list]:
    """
    宁波银行等：「四、成交内容」下竖排四列表头（序号、采购内容、数量、成交供应商）+ 一行数据。
    与「二、采购内容」六列表、「中标结果」两列表标题不同，互不抢匹配。
    """
    if not content or not isinstance(content, str):
        return content, []
    if _existing_has_chengjiao_four_col(existing_tables):
        return content, []
    lines = content.splitlines()
    headers = list(_CHENGJIAO_CONTENT_HEADERS)
    n = len(headers)
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if not ln or not re.match(
            r"^[一二三四五六七八九十]+[、,，．.]\s*成交内容", ln
        ):
            continue
        cur = _next_meaningful_line_idx(i + 1, lines)
        ok = True
        first_hdr = cur
        for exp in headers:
            if cur >= len(lines):
                ok = False
                break
            if lines[cur].strip() != exp:
                ok = False
                break
            cur = _next_meaningful_line_idx(cur + 1, lines)
        if not ok:
            continue
        vals: list[str] = []
        val0 = cur
        for _ in range(n):
            if cur >= len(lines):
                ok = False
                break
            v = lines[cur].strip()
            if not v:
                ok = False
                break
            if re.match(r"^[一二三四五六七八九十]+[、,，．.]", v) and "成交" not in v:
                ok = False
                break
            vals.append(v)
            last_v = cur
            cur = _next_meaningful_line_idx(cur + 1, lines)
        if not ok or len(vals) != n:
            continue
        if not vals[0].isdigit():
            continue
        new_lines = (
            lines[:first_hdr]
            + ["\t".join(headers), "\t".join(vals)]
            + lines[last_v + 1 :]
        )
        work = "\n".join(new_lines)
        tbl = [headers, vals]
        print("    [正文] 「成交内容」竖排四列表格已还原")
        return work, [tbl]
    return content, []


# 华润守正采购平台等：inner_text 压成「成交：」下竖排三列 + 一行数据
_SHOUZHENG_XUNYUAN_HEADERS = ("序号", "寻源单名称", "供应商名称")


def _existing_has_shouzheng_xunyuan_three_col(existing_tables: list) -> bool:
    want = list(_SHOUZHENG_XUNYUAN_HEADERS)
    for t in existing_tables or []:
        if not t or not t[0] or len(t[0]) < 3:
            continue
        r0 = [str(c or "").strip() for c in t[0][:3]]
        if r0 == want:
            return True
    return False


def repair_shouzheng_xunyuan_vertical_three_col_table(
    content: str, existing_tables: Optional[list] = None,
) -> tuple[str, list]:
    """
    华润信托/守正等：「成交：」下竖排「序号、寻源单名称、供应商名称」+ 一行数据（与「四、成交内容」四列互不抢）。
    """
    if not content or not isinstance(content, str):
        return content, []
    if _existing_has_shouzheng_xunyuan_three_col(existing_tables):
        return content, []
    lines = content.splitlines()
    headers = list(_SHOUZHENG_XUNYUAN_HEADERS)
    n = len(headers)
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if ln not in ("成交：", "成交:"):
            continue
        cur = _next_meaningful_line_idx(i + 1, lines)
        ok = True
        first_hdr = cur
        for exp in headers:
            if cur >= len(lines):
                ok = False
                break
            if lines[cur].strip() != exp:
                ok = False
                break
            cur = _next_meaningful_line_idx(cur + 1, lines)
        if not ok:
            continue
        vals: list[str] = []
        last_v = cur
        for _ in range(n):
            if cur >= len(lines):
                ok = False
                break
            v = lines[cur].strip()
            if not v:
                ok = False
                break
            if v.startswith("采购人：") or v.startswith("采购单位："):
                ok = False
                break
            if re.match(r"^[一二三四五六七八九十]+[、,，．.]", v) and len(v) < 12:
                ok = False
                break
            vals.append(v)
            last_v = cur
            cur = _next_meaningful_line_idx(cur + 1, lines)
        if not ok or len(vals) != n:
            continue
        if not vals[0].isdigit():
            continue
        new_lines = (
            lines[:first_hdr]
            + ["\t".join(headers), "\t".join(vals)]
            + lines[last_v + 1 :]
        )
        work = "\n".join(new_lines)
        tbl = [headers, vals]
        print("    [正文] 「成交」竖排三列（寻源单）表格已还原")
        return work, [tbl]
    return content, []


def text_blocks_from_detail_body(body: str) -> list:
    """
    与 extract_body_below_label 裁剪后的正文对齐，避免 text_blocks 仍含整页侧栏/列表 JSON。
    段落按空行切分；无空行则单条。
    """
    s = (body or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in s.split("\n\n") if p.strip()]
    return parts if len(parts) > 1 else [s]


def extract_body_below_label(content: str, label: str = "公告正文") -> str:
    """
    只保留「详情正文」或「公告正文」锚点以下的内容，去掉页面导航、按钮等噪音。
    采招网 Vue 详情常见「详情正文」；旧版为「公告正文」。若两者皆无，再尝试自定义 label。
    若找不到任何锚点，仍对全文做顶部 UI 行过滤（采招网详情区常含「中标追踪」等入口文案）。
    """
    if not content:
        return ""
    work = content.strip()
    if "详情正文" in work:
        work = work.split("详情正文", 1)[1].strip()
    elif "公告正文" in work:
        work = work.split("公告正文", 1)[1].strip()
    elif label in work:
        work = work.split(label, 1)[1].strip()
    # skip_ui 阶段（公告正文区顶部按钮区）需要过滤的 UI 行；「中标追踪」为站内功能入口，非公告原文
    ui_lines = {"收藏", "分享", "分享功能升级", "我知道了", "邮箱", "导出", "打印", "内容纠错",
                "现在可以直接通过小程序码将信息分享 给好友了！", "发送到邮箱", "为你推荐", "查看更多类似项目>>",
                "返回", "列表", "中标追踪"}
    # 实际内容开始后，遇到以下标志立即截断（推荐阅读及以下不获取）
    # 「招标进展」及之后为采招网站内时间轴/关联公告，非本条公告正文
    tail_cutoffs = {
        "为你推荐", "查看更多类似项目>>", "发送到邮箱", "推荐阅读", "相关推荐",
        "招标进展", "查看完整进展",
    }
    lines = work.split("\n")
    result = []
    skip_ui = True
    for line in lines:
        s = line.strip()
        # 招标进展：无论是否仍在 skip_ui 阶段，一律截断（避免时间轴被当成正文）
        if s == "招标进展":
            break
        if s == "查看完整进展" or (s.startswith("查看完整进展") and len(s) < 32):
            break
        # Vue 列表区整段 JSON（含多条 news_title_show），非本条公告正文
        if (
            not skip_ui
            and len(s) > 100
            and '"news_id"' in s
            and '"news_title_show"' in s
        ):
            break
        # ⚠️ tail_cutoffs 只在实际内容开始后才截断（skip_ui=False 之后）
        # 遇到「推荐阅读」或整行在截断集合里则不再获取后面内容
        if not skip_ui and (
            s in tail_cutoffs
            or "推荐阅读" in s
            or "相关推荐" in s
            or ("招标进展" in s and len(s) < 20)
        ):
            break
        if skip_ui and s in ui_lines:
            continue
        if skip_ui and not s:
            continue
        if skip_ui and len(s) > 15:
            skip_ui = False
        result.append(line)
    # 去掉末尾残留的「返回」「列表」「推荐阅读」等
    tail_strip = {
        "返回", "列表", "发送到邮箱", "分享", "推荐阅读", "相关推荐",
        "查看完整进展", "招标进展",
    }
    while result and result[-1].strip() in tail_strip:
        result.pop()
    return "\n".join(result).strip()


def _strip_html_after_recommend(html: str) -> str:
    """去掉 HTML 中「推荐阅读」及之后的内容，避免保存推荐区。"""
    if not html:
        return html
    pos = -1
    for marker in ("推荐阅读", "相关推荐"):
        i = html.find(marker)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return html
    # 从 marker 往前找到最后一个 '>'，避免截断在标签中间
    cut = html.rfind(">", 0, pos)
    if cut != -1:
        return html[: cut + 1].strip()
    return html[:pos].strip()


def save_to_file(info_type: str, title: str, url: str, content: str,
                 out_dir: str = OUTPUT_DIR) -> str:
    """文件名 = 信息类型_标题.txt，保存到关键词子目录"""
    os.makedirs(out_dir, exist_ok=True)
    raw_name = sanitize_filename(f"{info_type}_{title}") or \
               f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    filepath = os.path.join(out_dir, f"{raw_name}.txt")
    if os.path.exists(filepath):
        filepath = os.path.join(out_dir, f"{raw_name}_{datetime.now().strftime('%H%M%S')}.txt")

    header = (
        f"标题：{title}\n"
        f"类型：{info_type}\n"
        f"来源：{url}\n"
        f"抓取时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        + "=" * 60 + "\n\n"
    )
    Path(filepath).write_text(header + content, encoding="utf-8")
    print(f"    [已保存] {filepath}")
    return filepath


def save_to_html(info_type: str, title: str, url: str, content_html: str,
                 out_dir: str = OUTPUT_DIR, text_fallback: str = "") -> str:
    """保存为独立 HTML 文件，保留表格等结构。content_html 为空时用 text_fallback 包在 <pre> 里。"""
    os.makedirs(out_dir, exist_ok=True)
    raw_name = sanitize_filename(f"{info_type}_{title}") or \
               f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    filepath = os.path.join(out_dir, f"{raw_name}.html")
    if os.path.exists(filepath):
        filepath = os.path.join(out_dir, f"{raw_name}_{datetime.now().strftime('%H%M%S')}.html")

    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    fallback_escaped = (text_fallback or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = content_html.strip() if content_html.strip() else f"<pre>{fallback_escaped}</pre>"
    doc = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"UTF-8\">\n"
        f"<title>{safe_title}</title>\n"
        "<style>table{border-collapse:collapse;} th,td{border:1px solid #333;padding:6px 10px;} body{font-family:sans-serif;max-width:900px;margin:20px auto;}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{safe_title}</h1>\n"
        f"<p><strong>类型：</strong>{info_type} &nbsp; <strong>来源：</strong><a href=\"{url}\">{url}</a></p>\n"
        f"<p><strong>抓取时间：</strong>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>\n<hr>\n"
        f"<div class=\"article-body\">\n{body}\n</div>\n</body>\n</html>"
    )
    Path(filepath).write_text(doc, encoding="utf-8")
    print(f"    [已保存] {filepath}")
    return filepath


def save_to_json(info_type: str, title: str, url: str,
                 text_blocks: list, tables: list, content: str,
                 out_dir: str = OUTPUT_DIR) -> str:
    """
    将结构化内容保存为 JSON 文件，字段说明：
    - text_blocks : 段落文本列表
    - tables      : 二维表格列表 list[list[list[str]]]，完整保留表格结构
    - content     : 纯文本兜底（text_blocks 为空时参考用）
    """
    import json
    os.makedirs(out_dir, exist_ok=True)
    raw_name = sanitize_filename(f"{info_type}_{title}") or \
               f"document_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    filepath = os.path.join(out_dir, f"{raw_name}.json")
    if os.path.exists(filepath):
        filepath = os.path.join(out_dir, f"{raw_name}_{datetime.now().strftime('%H%M%S')}.json")

    data = {
        "title":       title,
        "info_type":   info_type,
        "url":         url,
        "crawl_time":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "text_blocks": text_blocks,
        "tables":      tables,
        "content":     content,
    }
    Path(filepath).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"    [已保存] {filepath}")
    return filepath


async def safe_screenshot(page, name: str):
    try:
        await page.screenshot(path=f"{name}.png", full_page=True, timeout=8000)
        print(f"  [截图] {name}.png")
    except Exception:
        pass


# ─────────────────────────────────────────────────────
# 步骤1：登录
# ─────────────────────────────────────────────────────

async def login(page):
    """
    登录采招网 SSO
    - 用户名：#txtusername
    - 密码：  #txtpassword
    - 登录：  JS 调用 pub.loginMember()（绕过按钮遮挡）
    """
    print(f"\n{'='*52}")
    print(f"  步骤1：登录")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await rand_sleep(page, 1000, 3000)   # 页面渲染随机等待

    await page.locator("#txtusername").fill(USERNAME)
    await rand_sleep(page, 200, 600)     # 填完用户名停顿
    await page.locator("#txtpassword").fill(PASSWORD)
    await rand_sleep(page, 300, 800)     # 填完密码停顿，模拟人工输入
    await page.evaluate("pub.loginMember()")

    try:
        await page.wait_for_url(
            lambda u: "sso.bidcenter.com.cn/login" not in u,
            timeout=25000,
        )
        print(f"  [OK] 登录成功 -> {page.url}")
    except PlaywrightTimeout:
        await safe_screenshot(page, "login_fail")
        raise RuntimeError("登录超时，请查看 login_fail.png（可能有验证码）")


# ─────────────────────────────────────────────────────
# 步骤2：主站搜索 → 获取新标签页
# ─────────────────────────────────────────────────────

async def open_search_tab(context, keyword: str):
    """
    在主站搜索框输入关键词，调用 jq_search() 打开新标签页，
    返回搜索结果标签页 page 对象。
    """
    home_page = await context.new_page()
    await home_page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    await rand_sleep(home_page, 1000, 3000)   # 主页渲染随机等待

    # 监听新标签页事件（jq_search 会 window.open 新标签页）
    async with context.expect_page(timeout=15000) as new_page_info:
        await home_page.locator("#aliSearchInput").fill(keyword)
        await rand_sleep(home_page, 200, 700)  # 输入关键词后停顿
        await home_page.evaluate("jq_search()")

    search_page = await new_page_info.value
    await search_page.wait_for_load_state("domcontentloaded", timeout=20000)
    await rand_sleep(search_page, 1000, 3000)  # 搜索结果页渲染随机等待
    await home_page.close()  # 关掉主站页，节省内存

    print(f"  [OK] 搜索结果页 -> {search_page.url}")
    return search_page


# ─────────────────────────────────────────────────────
# 步骤3：点击"标题搜索"
# ─────────────────────────────────────────────────────

async def click_title_search(page):
    """
    在搜索结果页找到并点击可见的"标题搜索"按钮。
    按钮结构：<li><a href="javascript:;">标题搜索</a></li>
    点击后等待页面重新加载。
    """
    # 找所有文字为"标题搜索"的 A 标签，取第一个可见的
    clicked = False
    candidates = await page.locator("a").all()
    for a in candidates:
        try:
            txt = (await a.inner_text()).strip()
            if txt == "标题搜索" and await a.is_visible(timeout=500):
                await a.click()
                clicked = True
                print("  [OK] 已点击「标题搜索」")
                break
        except Exception:
            continue

    if not clicked:
        print("  [WARN] 未找到「标题搜索」按钮，使用默认全文搜索")
        return

    # 等待结果刷新
    await page.wait_for_load_state("networkidle", timeout=15000)
    await rand_sleep(page, 1000, 3000)   # 标题搜索结果渲染随机等待
    print(f"  [OK] 标题搜索结果页 -> {page.url}")


# ─────────────────────────────────────────────────────
# 步骤3.5：应用时间筛选
# ─────────────────────────────────────────────────────

async def apply_date_filter(page, start_date: str, end_date: str):
    """
    在当前搜索结果 URL 上追加 time=5&stime&endtime 并重新加载。
    采招网实际参数：time=5 表示「自定义时间」，stime/endtime 格式 YYYY-MM-DD。
    留空则不做任何操作。
    """
    if not start_date and not end_date:
        return

    parsed = urlparse(page.url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    # time=5 表示自定义日期范围，否则站点可能忽略 stime/endtime
    params["time"] = ["5"]
    if start_date:
        params["stime"] = [start_date]
    if end_date:
        params["endtime"] = [end_date]

    new_query = urlencode({k: v[0] for k, v in params.items()})
    new_url = urlunparse(parsed._replace(query=new_query))

    print(f"  [时间筛选] {start_date} ~ {end_date}")
    await page.goto(new_url, wait_until="networkidle", timeout=20000)
    await rand_sleep(page, 800, 2500)    # 时间筛选后渲染随机等待
    print(f"  [OK] 时间筛选已应用 -> {new_url}")


# ─────────────────────────────────────────────────────
# 步骤3.6：应用搜索模式（精准/全文）
# ─────────────────────────────────────────────────────

async def apply_search_mode(page, search_mode: int):
    """
    在当前搜索结果 URL 上将 mod 参数设为指定值后重新加载。
    search_mode=0 全文搜索（默认），search_mode=1 精准搜索。
    mod 已经是目标值则跳过，不重复请求。
    """
    if search_mode == 0:
        return

    parsed = urlparse(page.url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    current_mod = params.get("mod", ["0"])[0]
    if current_mod == str(search_mode):
        return

    params["mod"] = [str(search_mode)]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    new_url = urlunparse(parsed._replace(query=new_query))

    mode_label = "精准搜索" if search_mode == 1 else f"mod={search_mode}"
    print(f"  [搜索模式] {mode_label}")
    await page.goto(new_url, wait_until="networkidle", timeout=20000)
    await rand_sleep(page, 800, 2000)
    # 截图 + 转储结果区 HTML，排查精准搜索下的 DOM 结构
    await safe_screenshot(page, "debug_search_mode")
    try:
        result_html = await page.locator(".ssjg-list").inner_html(timeout=5000)
        print(f"  [DEBUG] .ssjg-list HTML（前2000字）:\n{result_html[:2000]}")
    except Exception:
        try:
            body_html = await page.locator("body").inner_html(timeout=5000)
            print(f"  [DEBUG] body HTML（前3000字）:\n{body_html[:3000]}")
        except Exception:
            pass
    print(f"  [OK] 搜索模式已应用 -> {new_url}")


# ─────────────────────────────────────────────────────
# 步骤4：解析当前页的搜索结果列表
# ─────────────────────────────────────────────────────

def fix_url(href: str) -> str:
    """将 // 开头的协议相对 URL 转为 https: 绝对 URL，其余原样返回。"""
    if not href:
        return ""
    s = href.strip()
    if s.startswith("//"):
        return "https:" + s
    return s


async def get_result_links(page) -> list[dict]:
    """
    解析搜索结果列表。支持多种结构：
    1）按 .ssjg-list_cell 行 + 行内 a[tid]；
    2）若无则按 .ssjg-list 内所有详情链接；
    3）若仍无则等“条信息”出现后，在整页范围内抓 a[tid] / customDesSearch 链接（兼容无 .ssjg-list 的页面）。
    每项格式：{ url, title, info_type }
    """
    import re as _re

    # 类型规范化映射：仅处理平台 .ssjg-leixing 真实存在的非标准值
    # 平台已经直接返回「招标公告」「中标结果」「中标候选人公示」「流标」等标准值，无需转换
    # 只有以下极少数平台自身用的细分标签需要归一化：
    _TYPE_NORMALIZE = {
        "招标变更": "招标公告",   # 平台对"变更公告"打的标签，归入招标公告
        "招标预告": "招标公告",   # 平台对"预告"打的标签，归入招标公告
        "审批公示": "招标公告",   # 审批类公示，归入招标公告
        "采购信息": "招标公告",   # 泛采购信息，归入招标公告
    }

    def _normalize_type(t: str) -> str:
        return _TYPE_NORMALIZE.get(t, t)

    def _extract_type_from_title(raw_title: str):
        """从标题推断 info_type：
        1. 先剥离开头的 [xxx] 前缀（平台名/状态标注），得到干净标题
        2. 用关键词规则从干净标题推断类型
        3. 识别不出则返回「采招信息」
        """
        # 剥离开头 [xxx] 前缀
        clean = _re.sub(r"^\[[^\[\]]{1,20}\]", "", raw_title).strip()
        if not clean:
            clean = raw_title.strip()

        # 关键词优先级顺序推断（与 ai_analyze.SUB_TYPE_KEYWORDS 一致，并扩展）
        keywords = [
            ("流标",          "流标"),
            ("成交结果",       "成交结果"),
            ("中标候选人公示",  "中标候选人公示"),
            ("候选人公示",     "中标候选人公示"),
            ("中标公示",       "中标公示"),
            ("评标结果公示",    "评标结果公示"),
            ("中标结果公示",    "中标结果公示"),
            ("中标结果",       "中标结果"),
            ("中标公告",       "中标公告"),
            ("中标",          "中标"),
            ("竞争性磋商",     "竞争性磋商"),
            ("磋商",          "磋商"),
            ("竞争性谈判",     "竞争性谈判"),
            ("谈判",          "谈判"),
            ("询价",          "询价"),
            ("邀请招标",       "邀请招标"),
            ("招标公告",       "招标公告"),
            ("招标",          "招标"),
            ("公示",          "公示"),
            ("征集",          "征集"),
            ("邀请",          "邀请"),
            ("采购公告",       "采购公告"),
            ("采购",          "采购"),
        ]
        for kw, label in keywords:
            if kw in clean:
                return label, clean
        return "采招信息", clean


    # 先等“结果区”出现：优先 .ssjg-list，否则等页面出现“条信息”（说明结果数已展示）
    list_ok = False
    try:
        await page.wait_for_selector(".ssjg-list", timeout=12000)
        list_ok = True
    except PlaywrightTimeout:
        pass
    if not list_ok:
        try:
            await page.wait_for_selector("text=条信息", timeout=10000)
            print("  [DEBUG] 已检测到「条信息」，按整页详情链接解析")
        except PlaywrightTimeout:
            await safe_screenshot(page, "debug_no_list")
            print("  [WARN] 等待结果区（.ssjg-list 或 条信息）超时，本页可能无数据")
            return []
    await page.wait_for_timeout(2500)  # 给 AJAX 结果列表渲染时间

    links = []

    # 方式一：按行 .ssjg-list_cell 解析（不依赖父容器 .ssjg-list 是否存在），按 URL 去重
    # .ssjg-list_cell 是每条结果行，.ssjg-leixing 在其内部，是平台蓝色类型标签
    seen_in_page = set()
    try:
        items = await page.locator(".ssjg-list_cell").all()
        for item in items:
            try:
                # 直接取 .ssjg-leixing DOM 值（页面蓝色类型标签），这是最准确的来源
                # 超时 3000ms；抓不到则记为"采招信息"并打印警告，不做任何标题推断
                info_type = "采招信息"
                try:
                    leixing_el = item.locator(".ssjg-leixing").first
                    leixing = (await leixing_el.inner_text(timeout=3000)).strip()
                    if leixing:
                        info_type = _normalize_type(leixing)
                    else:
                        print("      [WARN] .ssjg-leixing 为空，info_type 将记为采招信息")
                except Exception:
                    print("      [WARN] 未找到 .ssjg-leixing，info_type 将记为采招信息")
                a_tag = item.locator("a[tid]").first
                tid = (await a_tag.get_attribute("tid") or "").strip()
                href = (await a_tag.get_attribute("href") or "").strip()
                title_raw = await a_tag.inner_text(timeout=2000)
                title = " ".join(title_raw.split())
                # 去掉标题里的平台/状态前缀 [xxx]，保留干净标题
                title = _re.sub(r"^\[[^\[\]]{1,20}\]", "", title).strip() or title
                title = unwrap_dom_double_glyphs(title, min_len=6)
                if not href and tid:
                    href = f"//user.bidcenter.com.cn/v2023/#/des/customDesSearch/{tid}"
                if not href or not title:
                    continue
                url = fix_url(href)
                if url in seen_in_page:
                    continue
                seen_in_page.add(url)
                links.append({"url": url, "title": title, "info_type": info_type})
            except Exception:
                continue
    except Exception:
        pass

    # 方式二：若方式一为 0 条，用“列表内”详情链接兜底
    if not links:
        try:
            anchors = await page.locator(
                ".ssjg-list a[tid], .ssjg-list a[href*='customDesSearch'], .ssjg-list a[href*='/des/']"
            ).all()
            seen_urls = set()
            for a in anchors:
                try:
                    href = (await a.get_attribute("href") or "").strip()
                    tid = (await a.get_attribute("tid") or "").strip()
                    title_raw = await a.inner_text(timeout=1500)
                    title = " ".join(title_raw.split())
                    title = unwrap_dom_double_glyphs(title, min_len=6)
                    if not title or len(title) < 2:
                        continue
                    if not href and tid:
                        href = f"//user.bidcenter.com.cn/v2023/#/des/customDesSearch/{tid}"
                    if not href:
                        continue
                    url = fix_url(href)
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    # 从标题前缀 [类型] 提取 info_type
                    info_type_2, title = _extract_type_from_title(title)
                    links.append({"url": url, "title": title, "info_type": info_type_2})
                except Exception:
                    continue
        except Exception as e:
            print(f"  [DEBUG] 兜底解析异常: {e}")

    # 方式三：无 .ssjg-list 或前两种为 0 条时，整页抓“详情链接”（标题像招标/中标的 a[tid] 或 customDesSearch）
    if not links:
        try:
            anchors = await page.locator(
                "a[tid], a[href*='customDesSearch'], a[href*='/des/']"
            ).all()
            seen_urls = set()
            for a in anchors:
                try:
                    if not await a.is_visible(timeout=500):
                        continue
                    href = (await a.get_attribute("href") or "").strip()
                    tid = (await a.get_attribute("tid") or "").strip()
                    title_raw = await a.inner_text(timeout=1500)
                    title = " ".join(title_raw.split())
                    title = unwrap_dom_double_glyphs(title, min_len=6)
                    if not title or len(title) < 4:
                        continue
                    if not href and tid:
                        href = f"//user.bidcenter.com.cn/v2023/#/des/customDesSearch/{tid}"
                    if not href:
                        continue
                    url = fix_url(href)
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    # 从标题前缀 [类型] 提取 info_type
                    info_type_3, title = _extract_type_from_title(title)
                    links.append({"url": url, "title": title, "info_type": info_type_3})
                except Exception:
                    continue
        except Exception as e:
            print(f"  [DEBUG] 整页链接兜底异常: {e}")

    if not links:
        await safe_screenshot(page, "debug_no_results")
        try:
            list_html = await page.locator(".ssjg-list").inner_html(timeout=3000)
            print(f"  [DEBUG] .ssjg-list 内容（前2500字）:\n{list_html[:2500]}")
        except Exception:
            try:
                body_snip = await page.content()
                print(f"  [DEBUG] body 片段（前3000字）:\n{body_snip[:3000]}")
            except Exception:
                pass

    print(f"  本页找到 {len(links)} 条结果")
    return links


# ─────────────────────────────────────────────────────
# 步骤5：翻页
# ─────────────────────────────────────────────────────

async def go_next_page(page) -> bool:
    """点击下一页，返回 True 表示成功翻页；无下一页或按钮不可用则返回 False"""
    for sel in [
        "a:has-text('下一页')", ".pagination .next", "li.next > a",
        "a[rel='next']", ".page-next",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                cls = (await btn.get_attribute("class") or "").lower()
                if "disabled" in cls or "grey" in cls:
                    break
                # 父元素（如 li）为 disabled 时也视为无下一页
                try:
                    parent = btn.locator("xpath=..")
                    if await parent.count() > 0:
                        pcls = (await parent.first.get_attribute("class") or "").lower()
                        if "disabled" in pcls or "grey" in pcls:
                            break
                except Exception:
                    pass
                await btn.click()
                try:
                    await page.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass
                await rand_sleep(page, 1500, 5000)   # 翻页后随机等待，避免高频翻页
                return True
        except Exception:
            continue
    return False


# ─────────────────────────────────────────────────────
# PDF 工具：检测 + 下载 + 解析
# ─────────────────────────────────────────────────────

def unwrap_pdfjs_url(url: str) -> str:
    """
    检测 PDF.js 查看器 URL（viewer.html?file=...），
    提取并返回真实 PDF 文件地址。
    - file= 有值 → 返回解码后的真实 PDF URL
    - file= 为空（PDF 由 JS 动态注入）→ 返回空字符串，由调用方跳过此 URL
    - 非 viewer URL → 原样返回
    """
    if "viewer.html" in url and "file=" in url:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        file_param = qs.get("file", [""])[0]
        if file_param:
            return unquote(file_param)
        return ""   # file= 为空，跳过这个 iframe
    return url


def _href_is_actionable(href: str) -> bool:
    s = (href or "").strip().lower()
    if not s or s.startswith("javascript:") or s in ("#", "javascript:void(0)", "javascript:void(0);"):
        return False
    return True


async def _js_click_visible_dialog_download(page, label: str) -> bool:
    """
    用 DOM 原生 click 点弹层内下载，绕过部分站点 el-dialog__wrapper 对 Playwright 命中测试的拦截。
    """
    try:
        return bool(await page.evaluate(
            """(label) => {
              const wrappers = [...document.querySelectorAll('.el-dialog__wrapper')];
              const vis = (el) => {
                if (!el) return false;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden'
                  && parseFloat(s.opacity || '1') > 0.05;
              };
              const wrap = wrappers.find(w => vis(w) && w.querySelector('.el-dialog'));
              if (!wrap) return false;
              const xz = wrap.querySelector('a.xiazai-btn, .xiazai-btn, [class*="xiazai-btn"]');
              if (xz) { xz.click(); return true; }
              const want = (label || '').trim();
              const cand = [...wrap.querySelectorAll('a,button')].filter(n => {
                const t = (n.innerText || n.textContent || '').trim();
                return t === want || (want && t.includes(want));
              });
              if (cand[0]) { cand[0].click(); return true; }
              return false;
            }""",
            label,
        ))
    except Exception:
        return False


async def _click_download_entry(page, text: str) -> None:
    """优先点 Element 弹层内的下载（含采招网 class=xiazai-btn），避免被遮罩挡点击。"""
    # 采招网附件弹层里常为 <a class="xiazai-btn" href="javascript:;">点击下载</a>
    dlg_xz = page.locator(".el-dialog:visible a.xiazai-btn, .el-dialog:visible .xiazai-btn").first
    try:
        if await dlg_xz.is_visible(timeout=2000):
            await dlg_xz.scroll_into_view_if_needed(timeout=5000)
            await dlg_xz.click(force=True, timeout=12000)
            print("    [附件] 已点击弹层内下载按钮(.xiazai-btn)")
            return
    except Exception:
        pass
    inner = page.locator(".el-dialog:visible").locator(
        f"a:has-text('{text}'), button:has-text('{text}')"
    ).first
    try:
        if await inner.is_visible(timeout=1500):
            await inner.scroll_into_view_if_needed(timeout=5000)
            await inner.click(force=True, timeout=12000)
            return
    except Exception:
        pass
    if await _js_click_visible_dialog_download(page, text):
        print("    [附件] 已用 JS 兜底点击弹层内下载")
        return
    outer = page.locator(f"a:has-text('{text}'), button:has-text('{text}')").first
    await outer.scroll_into_view_if_needed(timeout=5000)
    await outer.click(force=True, timeout=12000)


async def _read_playwright_download_as_pdf(dl) -> Optional[bytes]:
    """从 Playwright Download 对象读取临时文件，校验 PDF 魔数。"""
    try:
        path = await dl.path()
        if not path:
            return None
        data = Path(path).read_bytes()
        if data[:4] == b"%PDF" and len(data) > 200:
            return data
    except Exception:
        pass
    return None


async def try_download_attachment_bytes(page) -> Optional[bytes]:
    """
    部分详情页「点击下载 / 附件下载」触发 PDF：可能是浏览器下载，也可能是新标签直开 fileyun 预签名 URL。
    同时监听 download 事件与新开 page，再在超时后 fallback expect_download + 二次点击。
    """
    ctx = page.context
    # 先点开「附件下载」等区域，弹出 Element 附件列表后再点「点击下载」
    for opener in ("附件下载", "附件信息可下载", "条附件信息可下载"):
        try:
            op = page.locator(
                f"a:has-text('{opener}'), button:has-text('{opener}'), span:has-text('{opener}')"
            ).first
            if await op.is_visible(timeout=800):
                await op.click(force=True, timeout=5000)
                await page.wait_for_timeout(1500)
                print(f"    [附件] 已尝试展开附件区「{opener}」")
                break
        except Exception:
            continue

    labels = ("点击下载", "下载附件", "附件下载")
    for text in labels:
        dl_list: list = []
        new_pages: list = []

        def _on_dl(d):
            dl_list.append(d)

        def _on_pg(p):
            new_pages.append(p)

        try:
            outer = page.locator(f"a:has-text('{text}'), button:has-text('{text}')").first
            inner = page.locator(".el-dialog:visible").locator(
                f"a:has-text('{text}'), button:has-text('{text}')"
            ).first
            if not await outer.is_visible(timeout=800) and not await inner.is_visible(timeout=800):
                continue

            page.on("download", _on_dl)
            ctx.on("page", _on_pg)

            await _click_download_entry(page, text)

            deadline = time.monotonic() + 22.0
            while time.monotonic() < deadline:
                if dl_list:
                    data = await _read_playwright_download_as_pdf(dl_list[0])
                    if data:
                        print(f"    [附件] download 事件「{text}」捕获 PDF，共 {len(data)} 字节")
                        return data
                for p in list(new_pages):
                    try:
                        u = (p.url or "").strip()
                    except Exception:
                        continue
                    if not u or u == "about:blank":
                        continue
                    if ".pdf" not in u.lower():
                        continue
                    try:
                        await p.wait_for_load_state("commit", timeout=15000)
                    except Exception:
                        pass
                    try:
                        r = await ctx.request.get(u, timeout=90000)
                        if r.ok:
                            body = await r.body()
                            if body[:4] == b"%PDF" and len(body) > 200:
                                print(f"    [附件] 新标签「{text}」GET PDF，共 {len(body)} 字节")
                                try:
                                    await p.close()
                                except Exception:
                                    pass
                                return body
                    except Exception as exg:
                        print(f"    [附件] 新标签 GET PDF 失败: {exg}")
                await asyncio.sleep(0.12)

            try:
                async with page.expect_download(timeout=35000) as dl_info:
                    await _click_download_entry(page, text)
                dl = await dl_info.value
                data = await _read_playwright_download_as_pdf(dl)
                if data:
                    print(f"    [附件] expect_download「{text}」捕获 PDF，共 {len(data)} 字节")
                    return data
            except Exception as ex:
                print(f"    [附件] expect_download「{text}」: {ex}")
        finally:
            try:
                page.remove_listener("download", _on_dl)
            except Exception:
                pass
            try:
                ctx.remove_listener("page", _on_pg)
            except Exception:
                pass
            for p in list(new_pages):
                try:
                    if not p.is_closed():
                        await p.close()
                except Exception:
                    pass
    return None


async def find_pdf_url(page) -> str:
    """
    在页面中查找 PDF 来源 URL，优先级：
    1. <embed src> 或 <iframe src>（URL 含 .pdf 或 content-type 为 pdf）
    2. <a href>（URL 含 .pdf）
    3. 文字链接：点击下载 / 点击查看或下载公告文件 / 下载附件 等
    4. 拦截「点击下载」按钮触发的网络请求，捕获真实 PDF URL
    返回找到的第一个有效 PDF URL，找不到返回空字符串。
    """
    # ① embed / iframe src（含 .pdf 或 viewer.html?file=有值）
    for tag, attr in [("embed", "src"), ("iframe", "src")]:
        try:
            elems = await page.locator(tag).all()
            for el in elems:
                val = (await el.get_attribute(attr) or "").strip()
                if not val:
                    continue
                if ".pdf" in val.lower() or "viewer.html" in val:
                    real = unwrap_pdfjs_url(fix_url(val))
                    if real:       # file= 为空时 unwrap 返回 ""，跳过继续找
                        return real
        except Exception:
            continue

    # ② <a href> 含 .pdf
    try:
        elems = await page.locator("a").all()
        for el in elems:
            val = (await el.get_attribute("href") or "").strip()
            if val and ".pdf" in val.lower():
                real = unwrap_pdfjs_url(fix_url(val))
                if real:
                    return real
    except Exception:
        pass

    # ③ 文字链接（不限 .pdf 后缀，外部平台链接也收录）
    download_hints = [
        "点击下载", "下载附件", "附件下载",
        "点击查看或下载公告文件", "下载公告文件", "查看公告文件", "公告附件",
        "点击查看>>", "点击查看",
        "内容详见附件", "详情请见附件", "详情见附件", "详见附件", "详情请点击查看",
        "点击查看附件", "请点击查看", "点击查看内容",
    ]
    for hint in download_hints:
        try:
            el = page.locator(f"a:has-text('{hint}'), button:has-text('{hint}')").first
            if await el.is_visible(timeout=1000):
                href = (await el.get_attribute("href") or "").strip()
                if href and _href_is_actionable(href):
                    real = unwrap_pdfjs_url(fix_url(href))
                    return real if real else fix_url(href)
        except Exception:
            continue

    # ④ 点击「点击下载」并拦截弹出的请求 URL（部分采招网附件走 JS 跳转）
    try:
        btn = page.locator("a:has-text('点击下载'), button:has-text('点击下载')").first
        if await btn.is_visible(timeout=1000):
            captured: list = []

            def _on_request(req):
                u = req.url
                if ".pdf" in u.lower() or "download" in u.lower():
                    captured.append(u)

            page.on("request", _on_request)
            try:
                async with page.context.expect_page(timeout=12000) as popup_info:
                    await _click_download_entry(page, "点击下载")
                popup = await popup_info.value
                pdf_url_from_popup = popup.url
                await popup.close()
                if (
                    pdf_url_from_popup
                    and pdf_url_from_popup != "about:blank"
                    and _href_is_actionable(pdf_url_from_popup)
                ):
                    return pdf_url_from_popup
                if captured:
                    u0 = captured[0]
                    if u0 and _href_is_actionable(u0):
                        return u0
            finally:
                try:
                    page.remove_listener("request", _on_request)
                except Exception:
                    pass
    except Exception:
        pass

    return ""


PDF_SCAN_ONLY_MSG = "[PDF 无可提取文本，可能是扫描版图片]"


def _parse_pdf_bytes(raw: bytes) -> str:
    """用 pdfplumber 解析 PDF 字节，返回全文文本；失败时降级 PyMuPDF。"""
    # 优先：pdfplumber（基于 pdfminer）
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            pages_text = []
            for i, p in enumerate(pdf.pages, 1):
                t = p.extract_text() or ""
                if t.strip():
                    pages_text.append(f"[第{i}页]\n{t.strip()}")
            return "\n\n".join(pages_text) if pages_text else PDF_SCAN_ONLY_MSG
    except Exception as e:
        print(f"    [PDF] pdfplumber 解析失败({e})，降级 PyMuPDF...")

    # 降级：PyMuPDF（对加密/非标 PDF 兼容性更好）
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        pages_text = []
        for i in range(len(doc)):
            page = doc.load_page(i)
            t = (page.get_text("text") or "").strip()
            # 部分平台 PDF 用「文字块」排版，纯 text 模式字很少；再试 blocks
            if len(t) < 40:
                blocks = page.get_text("blocks") or []
                parts = []
                for b in blocks:
                    if len(b) >= 5 and b[4]:
                        s = str(b[4]).strip()
                        if s:
                            parts.append(s)
                alt = "\n".join(parts)
                if len(alt) > len(t):
                    t = alt.strip()
            if t:
                pages_text.append(f"[第{i+1}页]\n{t}")
        doc.close()
        return "\n\n".join(pages_text) if pages_text else PDF_SCAN_ONLY_MSG
    except Exception as e2:
        print(f"    [PDF] PyMuPDF 解析也失败({e2})")
        return PDF_SCAN_ONLY_MSG


def _pdf_pages_to_images(raw: bytes) -> list:
    """将 PDF 每页渲染为 PNG 字节列表，用于扫描版 OCR。无 PyMuPDF 时返回 []。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []
    out = []
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        for i in range(len(doc)):
            page = doc.load_page(i)
            # 2 倍分辨率便于 OCR 识别
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out.append(pix.tobytes("png"))
        doc.close()
    except Exception:
        return []
    return out


# 文本抽取过短（如只有水印「扫描全能王 创建」）时也走 OCR，避免漏掉多页扫描内容
PDF_MIN_TEXT_LEN = 80


async def _parse_pdf_raw(raw: bytes) -> str:
    """解析 PDF 字节：先文本抽取，若为扫描版或正文过短则转图片 OCR。"""
    text = _parse_pdf_bytes(raw)
    need_ocr = (
        text.strip() == PDF_SCAN_ONLY_MSG
        or len(text.strip()) < PDF_MIN_TEXT_LEN
    )
    if not need_ocr:
        return text
    images = _pdf_pages_to_images(raw)
    if images:
        print(f"    [PDF] 扫描版/少字 PDF 共 {len(images)} 页，转为图片 OCR...")
        ocr_text = await _ocr_images(images)
        # 勿把「未装 OCR」提示当作正文写进详情（此前会因字符串更长而误用）
        if ocr_text and ocr_text.strip().startswith("[OCR 不可用]"):
            hint = (
                "\n\n[说明] 该公告为 PDF 展示（多为扫描页或图片页），需 OCR 才能自动识别全文。"
                "\n请在本机执行：pip install rapidocr-onnxruntime"
                "\n安装后重新爬取本条即可。"
            )
            return (text if text.strip() else PDF_SCAN_ONLY_MSG) + hint
        if ocr_text and len(ocr_text) > len(text):
            return ocr_text
    return text


# OCR 引擎全局缓存
_ocr_engine = None


def _get_ocr_engine():
    """懒加载 RapidOCR（基于 ONNX Runtime，无 PyTorch 依赖）。"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            print("    [OCR] 初始化 RapidOCR...")
            _ocr_engine = RapidOCR()
            print("    [OCR] 初始化完成")
        except ImportError:
            raise RuntimeError(
                "RapidOCR 未安装，请运行：pip install rapidocr-onnxruntime"
            )
    return _ocr_engine


async def _ocr_images(captured_images: list) -> str:
    """对捕获的图片列表（bytes）执行 OCR，返回拼接文字。"""
    try:
        engine = _get_ocr_engine()
    except RuntimeError as e:
        return f"[OCR 不可用] {e}"

    import numpy as np
    from PIL import Image

    all_text = []
    for i, img_bytes in enumerate(captured_images, 1):
        print(f"    [OCR] 识别第 {i}/{len(captured_images)} 页 ...")
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img_np = np.array(img)
            result, _ = engine(img_np)
            if result:
                page_text = "\n".join(line[1] for line in result if line[1].strip())
                if page_text:
                    all_text.append(f"[第{i}页]\n{page_text}")
        except Exception as ex:
            print(f"    [OCR] 第 {i} 页识别异常: {ex}")

    if all_text:
        print(f"    [OCR] 全部 {len(all_text)} 页识别完成")
        return "\n\n".join(all_text)
    return ""


def _img_tag_is_probably_icon(tag: str) -> bool:
    """跳过明显装饰/占位小图（避免无意义 OCR）。"""
    if not tag:
        return True
    for attr in ("width", "height"):
        m = re.search(rf"\b{attr}\s*=\s*[\"']?(\d+)", tag, re.I)
        if m and int(m.group(1), 10) <= 2:
            return True
    return False


def _img_tag_attr_urls(tag: str) -> list[str]:
    """从单个 <img …> 标签串中取出可能的资源地址（懒加载常见 data-src）。"""
    out: list[str] = []
    for pat in (
        r'\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))',
        r"\bdata-src\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        r"\bdata-original\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        r"\bdata-url\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    ):
        sm = re.search(pat, tag, re.I)
        if not sm:
            continue
        u = (sm.group(1) or sm.group(2) or sm.group(3) or "").strip()
        if u and not u.startswith("javascript:"):
            out.append(u)
    return out


def _extract_inline_img_srcs(html: str) -> list[str]:
    """从正文 HTML 中收集 <img> 的 src / data-src 等，去重、保序。"""
    if not html or "<img" not in html.lower():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"<img\b[^>]*>", html, flags=re.I):
        tag = m.group(0)
        if _img_tag_is_probably_icon(tag):
            continue
        for src in _img_tag_attr_urls(tag):
            if src.startswith("data:image") and len(src) < 800:
                continue
            low = src.lower()
            if any(x in low for x in ("favicon", "spacer.gif", "blank.gif", "pixel", "beacon")):
                continue
            if src not in seen:
                seen.add(src)
                out.append(src)
    return out


_ARTICLE_IMG_SELECTORS = (
    ".article-content",
    ".detail-content",
    ".content-body",
    ".main-content",
    ".news-content",
    "#content",
    ".bid-content",
    ".view-content",
    "article",
    ".detail",
    ".des-content",
    ".announcement-detail",
    ".rich-text",
    ".ql-editor",
    # 不含 .el-main / .app-main 等整页布局：配图 OCR 宁可不做也不扫侧栏
)

# 爬取正文若命中整页容器，配图 OCR 不再以该选择器为根（避免侧栏图）
_OCR_SKIP_PRIMARY_FOR_IMAGES = frozenset(
    {".el-main", ".app-main", ".layout-content", ".main-container"}
)


async def _cleanup_announce_body_marker(fr, token: str) -> None:
    if not token:
        return
    try:
        await fr.evaluate(
            """(tok) => {
                document.querySelectorAll('[data-ocr-announce-body="' + tok + '"]').forEach(
                    (e) => e.removeAttribute('data-ocr-announce-body')
                );
            }""",
            token,
        )
    except Exception:
        pass


async def _resolve_announcement_body_root(fr):
    """
    用「公告正文」「详情正文」锚点向上找同时含「一、」或「二、」的正文块，仅在此子树内收图。
    返回 (Locator|None, marker_token)；无 token 表示未找到。
    """
    for label in ("公告正文", "详情正文"):
        loc = fr.get_by_text(label, exact=True)
        try:
            if await loc.count() == 0:
                continue
        except Exception:
            continue
        handle = None
        ok = False
        token = ""
        try:
            handle = await loc.first.element_handle()
            if not handle:
                continue
            token = "ab" + uuid.uuid4().hex[:14]
            ok = await fr.evaluate(
                """([start, tok]) => {
                    const T = (n) => (n.innerText || '').trim();
                    let n = start;
                    for (let i = 0; i < 20 && n; i++) {
                        const t = T(n);
                        if ((t.includes('一、') || t.includes('二、')) && t.length > 50) {
                            n.setAttribute('data-ocr-announce-body', tok);
                            return true;
                        }
                        n = n.parentElement;
                    }
                    return false;
                }""",
                [handle, token],
            )
        except Exception:
            ok = False
        finally:
            if handle:
                try:
                    await handle.dispose()
                except Exception:
                    pass
        if ok and token:
            root = fr.locator(f'[data-ocr-announce-body="{token}"]').first
            try:
                if await root.count() > 0:
                    return root, token
            except Exception:
                pass
            await _cleanup_announce_body_marker(fr, token)
    return None, ""


async def _collect_imgs_from_locator(root) -> list[str]:
    """在给定 Locator 根下收集 img 的 URL（去重保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    try:
        n = await root.locator("img").count()
    except Exception:
        return []
    for i in range(min(n, 40)):
        el = root.locator("img").nth(i)
        for attr in (
            "src",
            "data-src",
            "data-original",
            "data-url",
            "data-lazy-src",
        ):
            try:
                u = await el.get_attribute(attr)
            except Exception:
                u = None
            if not (u or "").strip():
                continue
            u = u.strip()
            if u.startswith("javascript:") or u.startswith("data:image"):
                continue
            low = u.lower()
            if any(x in low for x in ("favicon", "spacer.gif", "blank.gif", "pixel")):
                continue
            if u not in seen:
                seen.add(u)
                out.append(u)
            break
    return out


async def _screenshot_imgs_under_locator(root, *, min_image_bytes: int) -> list[bytes]:
    """仅对 root 内的 img / root 整块截图（公告正文内）。"""
    out: list[bytes] = []
    try:
        n = await root.locator("img").count()
    except Exception:
        return []
    for ii in range(min(n, 16)):
        el = root.locator("img").nth(ii)
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            png = await el.screenshot(timeout=10000)
            if png and len(png) >= min_image_bytes:
                out.append(png)
                print(f"    [配图OCR] 公告正文内 img 截图第 {len(out)} 张 ({len(png) // 1024}KB)")
        except Exception:
            continue
    if out:
        return out
    try:
        await root.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        box = await root.bounding_box()
        if box and float(box.get("height") or 0) > 120:
            png2 = await root.screenshot(timeout=12000)
            if png2 and len(png2) >= min_image_bytes:
                out.append(png2)
                print(f"    [配图OCR] 公告正文块整段截图 ({len(png2) // 1024}KB)")
    except Exception:
        pass
    return out


async def _collect_dom_article_img_urls_in_frame(fr, selectors: tuple) -> list[str]:
    """在单个 frame 内收集正文区 img 的 URL（采招网详情常在 iframe）。"""
    seen: set[str] = set()
    out: list[str] = []
    for sel in selectors:
        try:
            loc = fr.locator(sel)
            if await loc.count() == 0:
                continue
            root = loc.first
            n = await root.locator("img").count()
            for i in range(min(n, 30)):
                el = root.locator("img").nth(i)
                for attr in (
                    "src",
                    "data-src",
                    "data-original",
                    "data-url",
                    "data-lazy-src",
                ):
                    try:
                        u = await el.get_attribute(attr)
                    except Exception:
                        u = None
                    if not (u or "").strip():
                        continue
                    u = u.strip()
                    if u.startswith("javascript:") or u.startswith("data:image"):
                        continue
                    low = u.lower()
                    if any(x in low for x in ("favicon", "spacer.gif", "blank.gif", "pixel")):
                        continue
                    if u not in seen:
                        seen.add(u)
                        out.append(u)
                    break
        except Exception:
            continue
    return out


async def _collect_dom_article_img_urls(
    page, selectors: Optional[tuple] = None,
) -> list[str]:
    """遍历所有 frame（含 iframe 内详情），合并正文区图片 URL。"""
    sel_tuple = selectors if selectors else _ARTICLE_IMG_SELECTORS
    seen_all: set[str] = set()
    merged: list[str] = []
    for fr in page.frames:
        fu = (fr.url or "").strip()
        if fu.startswith("chrome-extension:"):
            continue
        try:
            part = await _collect_dom_article_img_urls_in_frame(fr, sel_tuple)
        except Exception:
            continue
        for u in part:
            if u not in seen_all:
                seen_all.add(u)
                merged.append(u)
    return merged


async def _screenshot_article_inline_images(
    page,
    *,
    selectors: Optional[tuple] = None,
    min_image_bytes: int = 800,
) -> list[bytes]:
    """跨 frame 截取正文容器内 <img> 像素（下载失败时的兜底）。"""
    sel_tuple = selectors if selectors else _ARTICLE_IMG_SELECTORS
    out: list[bytes] = []
    for fr in page.frames:
        fu = (fr.url or "").strip()
        if fu.startswith("chrome-extension:"):
            continue
        for sel in sel_tuple:
            try:
                loc = fr.locator(sel)
                if await loc.count() == 0:
                    continue
                root = loc.first
                n = await root.locator("img").count()
                for ii in range(min(n, 12)):
                    el = root.locator("img").nth(ii)
                    try:
                        await el.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    try:
                        png = await el.screenshot(timeout=10000)
                        if png and len(png) >= min_image_bytes:
                            out.append(png)
                            print(
                                f"    [配图OCR] 正文 img 截图第 {len(out)} 张 ({len(png) // 1024}KB)"
                            )
                    except Exception:
                        continue
                if out:
                    return out
                # 表格有时是 div 排版/背景图，或 img 截图为空：截取正文容器整块
                try:
                    await root.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    box = await root.bounding_box()
                    if box and float(box.get("height") or 0) > 120:
                        png2 = await root.screenshot(timeout=12000)
                        if png2 and len(png2) >= min_image_bytes:
                            out.append(png2)
                            print(
                                f"    [配图OCR] 正文容器整段截图 ({len(png2) // 1024}KB)"
                            )
                            return out
                except Exception:
                    pass
            except Exception:
                continue
    return out


def _inline_img_url_is_probably_ui_asset(url: str) -> bool:
    """采招网 Vue 侧栏/顶栏/推广等静态图，宁可不做 OCR 也不当正文配图。"""
    low = (url or "").lower()
    needles = (
        "/v2023/static/",
        "/static/img/app.",
        "/static/img/share.",
        "/static/img/email.",
        "mpaypass.com",
        "czwfwptwx",
        "favicon",
        "spacer.gif",
        "blank.gif",
        "pixel.gif",
        "beacon",
        "/img/weixin",
        "/img/qq",
        "qrcode",
        "wx_qrcode",
        "logo.png",
        "logo.jpg",
    )
    return any(x in low for x in needles)


def _resolve_resource_url(page_url: str, src: str) -> str:
    s = (src or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        return "https:" + s
    return urljoin(page_url or "", s)


def _merge_body_selectors(primary: Optional[str]) -> tuple:
    """配图 OCR 用：命中整页布局选择器时不作为 img 根，避免侧栏图。"""
    p = (primary or "").strip()
    if not p or p in _OCR_SKIP_PRIMARY_FOR_IMAGES:
        return _ARTICLE_IMG_SELECTORS
    rest = tuple(s for s in _ARTICLE_IMG_SELECTORS if s != p)
    return (p,) + rest


async def ocr_inline_images_from_article_html(
    page,
    html: str,
    *,
    primary_selector: Optional[str] = None,
    max_images: int = 20,
    min_image_bytes: int = 800,
) -> str:
    """
    只处理「公告正文 / 详情正文」锚点向上定位到的正文块内的图片；
    若页面上有该锚点：不解析整页 inner_html 里的图、不用整页布局下的 DOM 图。
    无锚点时回退为收窄后的正文 class；明显 UI 图 URL 跳过；不做整页大图兜底。
    """
    try:
        await page.wait_for_timeout(1200)
    except Exception:
        pass
    sel_order = _merge_body_selectors(primary_selector)
    seen: set[str] = set()
    srcs: list[str] = []
    roots_for_shot: list = []
    marker_cleanup: list[tuple[object, str]] = []
    strict_hit = False
    for fr in page.frames:
        fu = (fr.url or "").strip()
        if fu.startswith("chrome-extension:"):
            continue
        root, tok = await _resolve_announcement_body_root(fr)
        if not tok or root is None:
            continue
        try:
            if await root.count() == 0:
                await _cleanup_announce_body_marker(fr, tok)
                continue
        except Exception:
            await _cleanup_announce_body_marker(fr, tok)
            continue
        strict_hit = True
        marker_cleanup.append((fr, tok))
        roots_for_shot.append(root)
        for u in await _collect_imgs_from_locator(root):
            if u not in seen:
                seen.add(u)
                srcs.append(u)
        try:
            inner = await root.inner_html(timeout=2500)
            for u in _extract_inline_img_srcs(inner or ""):
                if u not in seen:
                    seen.add(u)
                    srcs.append(u)
        except Exception:
            pass

    if not strict_hit:
        for u in _extract_inline_img_srcs(html or ""):
            if u not in seen:
                seen.add(u)
                srcs.append(u)
        for u in await _collect_dom_article_img_urls(page, selectors=sel_order):
            if u not in seen:
                seen.add(u)
                srcs.append(u)

    base = (page.url or "").strip() or "https://www.bidcenter.com.cn/"
    referer = base
    blobs: list[bytes] = []
    try:
        for i, src in enumerate(srcs[:max_images], 1):
            full = _resolve_resource_url(base, src)
            if not full:
                continue
            if _inline_img_url_is_probably_ui_asset(full):
                continue
            try:
                resp = await page.request.get(
                    full,
                    timeout=25000,
                    headers={
                        "Referer": referer,
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                )
                if resp.status != 200:
                    continue
                body = await resp.body()
                if not body or len(body) < min_image_bytes:
                    continue
                ct = (resp.headers.get("content-type") or "").lower()
                if ct and "image" not in ct and "octet-stream" not in ct:
                    continue
                blobs.append(body)
                print(f"    [配图OCR] 已拉取第 {len(blobs)} 张图 ({len(body) // 1024}KB) {full[:72]}…")
            except Exception as ex:
                print(f"    [配图OCR] 下载失败 ({i}): {full[:80]} — {ex}")
        if not blobs:
            if roots_for_shot:
                for root in roots_for_shot:
                    try:
                        blobs.extend(
                            await _screenshot_imgs_under_locator(
                                root, min_image_bytes=min_image_bytes
                            )
                        )
                    except Exception:
                        continue
                    if blobs:
                        break
            # 已命中「公告正文」块时不再用其它 class 去截整栏，避免非正文图
            if not blobs and not strict_hit:
                blobs.extend(
                    await _screenshot_article_inline_images(
                        page, selectors=sel_order, min_image_bytes=min_image_bytes
                    )
                )
        if not blobs:
            print("    [配图OCR] 未获得可用图片（无 URL 或下载失败且正文 img 截图过小/无图）")
            return ""
        ocr_text = await _ocr_images(blobs)
        if not ocr_text or ocr_text.strip().startswith("[OCR 不可用]"):
            return ""
        return ocr_text
    finally:
        for fr, tok in marker_cleanup:
            await _cleanup_announce_body_marker(fr, tok)


async def _wait_for_capture(popup, captured: list, target: int, max_wait: int = 10):
    """等待 captured 列表长度达到 target，最多等 max_wait 秒。"""
    for _ in range(max_wait):
        if len(captured) >= target:
            return True
        await popup.wait_for_timeout(1000)
    return len(captured) >= target


async def _navigate_popup_page(popup, pg: int):
    """在 Aspose 查看器 popup 中翻到指定页。"""
    try:
        await popup.evaluate(f"commonFuncs.next()")
        await popup.wait_for_timeout(1500)
    except Exception:
        pass
    for sel in ["#nextBtn", "#mainRight a"]:
        try:
            btn = popup.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await popup.wait_for_timeout(1500)
                return
        except Exception:
            continue


async def _screenshot_popup_pages(popup, total_pages: int) -> list:
    """截图 fallback：对 Aspose 查看器每一页截图，返回 PNG bytes 列表。"""
    screenshots = []

    async def _take_one():
        """截取当前可见页面的 pageDiv 区域。"""
        for div_id in [f"pageDiv_{len(screenshots)+1}", "mainMiddle"]:
            try:
                el = popup.locator(f"#{div_id}").first
                if await el.is_visible(timeout=2000):
                    png = await el.screenshot(timeout=8000)
                    if png and len(png) > 1000:
                        return png
            except Exception:
                continue
        try:
            return await popup.screenshot(full_page=False, timeout=8000)
        except Exception:
            return None

    # 截图前彻底禁用水印：
    # 1. 关闭水印开关，阻止翻页时重新生成
    # 2. 覆盖 reSetWaterMark 为空函数，防止任何触发
    # 3. 删除已有水印 DOM
    try:
        await popup.evaluate("""(() => {
            if(typeof waterMarkConfig !== 'undefined') waterMarkConfig.showWaterMark = 'false';
            if(typeof newOpen !== 'undefined' && newOpen.reSetWaterMark) newOpen.reSetWaterMark = function(){};
            document.querySelectorAll('.mask_div').forEach(el => el.remove());
        })()""")
    except Exception:
        pass

    # 第1页
    await popup.wait_for_timeout(2000)
    shot = await _take_one()
    if shot:
        screenshots.append(shot)
        print(f"    [截图] 第 1/{total_pages} 页截图成功")

    # 翻页截图剩余页
    for pg in range(2, total_pages + 1):
        await _navigate_popup_page(popup, pg)
        await popup.wait_for_timeout(1500)
        shot = await _take_one()
        if shot:
            screenshots.append(shot)
            print(f"    [截图] 第 {pg}/{total_pages} 页截图成功")

    return screenshots


async def try_read_aspose_viewer_from_page_iframes(page) -> str:
    """
    部分站点「点击下载」不 window.open，而在当前页 iframe 里加载 Aspose/readView。
    expect_popup / expect_page 超时或失败后调用本函数。
    """
    await page.wait_for_timeout(2500)
    print("    [OCR] 尝试页内主文档 / iframe 回退（无新窗口时）…")
    # 主文档内嵌（Vue 路由不切 URL 时常见）
    try:
        main_html = await page.locator("html").inner_html(timeout=20000)
        if "totalPageNum" in main_html:
            print("    [OCR] 主文档 HTML 含 totalPageNum，按 Aspose 路径提取")
            return await extract_aspose_viewer_text(page, main_html, page.url)
    except Exception:
        pass

    for fr in page.frames:
        fu = fr.url or ""
        try:
            html = await fr.locator("html").inner_html(timeout=20000)
        except Exception:
            continue
        if "totalPageNum" not in html:
            continue
        print(f"    [OCR] 页内 iframe Aspose，URL 片段: {fu[:120]!r} …")
        try:
            return await extract_aspose_viewer_text(page, html, fu or page.url)
        except Exception as ex:
            print(f"    [OCR] iframe Aspose 提取失败: {ex}")
    return ""


async def ocr_via_popup_click(page) -> str:
    """
    在 bidcenter 详情页上点击「点击查看或下载公告文件」等链接，
    捕获弹出的 Aspose 查看器 popup。

    两步策略：
    1. 优先拦截 readView 图片网络响应（高质量原图）
    2. 拦截失败则 fallback 到截图模式（截取 popup 页面 DOM 元素）

    全部图片/截图送 RapidOCR 识别。
    """
    # 采招网「详见附件」类：优先点「点击下载 / 附件下载」，避免先点到其它「点击查看」弱相关链
    hints = [
        "点击下载", "附件下载", "下载附件",
        "点击查看或下载公告文件", "查看公告文件", "下载公告文件",
        "点击查看>>", "点击查看",
        "内容详见附件", "详情请见附件", "详情见附件", "详见附件",
        "详情请点击查看", "点击查看附件", "请点击查看", "点击查看内容",
    ]
    # 找到第一个可见的入口链接
    target_el = None
    for hint in hints:
        try:
            el = page.locator(f"a:has-text('{hint}'), button:has-text('{hint}')").first
            if await el.is_visible(timeout=1000):
                target_el = el
                print(f"    [OCR] 找到入口链接「{hint}」")
                break
        except Exception:
            continue

    if target_el is None:
        return ""

    # 只尝试一次：打开 popup → 拦截图片 → 截图 fallback → OCR
    captured: list = []
    ctx = page.context

    async def _on_ctx_resp(resp):
        url = resp.url
        if "readView" not in url and "gxOpenserviceNews" not in url:
            return
        ct = (resp.headers.get("content-type") or "").lower()
        if "html" in ct or "javascript" in ct:
            return
        try:
            data = await resp.body()
            if data and len(data) > 500:
                captured.append(data)
                print(f"    [OCR] 拦截到图片 {len(captured)}"
                      f"（{len(data)//1024}KB）")
        except Exception:
            pass

    ctx.on("response", _on_ctx_resp)

    try:
        # 与 try_download 一致：先展开附件弹层，再点入口（否则外层链被 el-dialog 挡住）
        for opener in ("附件下载", "附件信息可下载"):
            try:
                op = page.locator(
                    f"a:has-text('{opener}'), button:has-text('{opener}'), span:has-text('{opener}')"
                ).first
                if await op.is_visible(timeout=800):
                    await op.click(force=True, timeout=5000)
                    await page.wait_for_timeout(1500)
                    print(f"    [OCR] 已尝试展开附件区「{opener}」")
                    break
            except Exception:
                continue
        try:
            inner_hit = page.locator(
                ".el-dialog:visible a.xiazai-btn, .el-dialog:visible a:has-text('点击下载')"
            ).first
            if await inner_hit.is_visible(timeout=2500):
                target_el = inner_hit
                print("    [OCR] 改用弹层内下载入口")
        except Exception:
            pass
        print(f"    [OCR] 点击入口，等待新标签页（含 target=_blank / window.open）…")
        # expect_popup 在部分 Chromium/路由下不触发；expect_page 更宽，能抓到同一 context 新开页
        async with ctx.expect_page(timeout=25000) as new_page_info:
            await target_el.click(force=True, timeout=12000)
        popup = await new_page_info.value
        await popup.wait_for_load_state("domcontentloaded", timeout=20000)

        popup_html = await popup.content()
        m_total = re.search(
            r'totalPageNum\s*=\s*parseInt\(["\'](\d+)["\']', popup_html
        )
        total_pages = int(m_total.group(1)) if m_total else 4

        # 非 Aspose 弹窗（如「点击查看>>」跳转到中国招标投标公共服务平台等）：尝试 PDF 或正文
        if not m_total:
            print(f"    [OCR] 弹窗非 Aspose 查看器，尝试 PDF 或正文提取...")
            try:
                pdf_url = await find_pdf_url(popup)
                if pdf_url and _href_is_actionable(pdf_url):
                    pdf_text = await download_and_parse_pdf(popup, pdf_url)
                    if pdf_text and "PDF 地址" not in pdf_text:
                        await popup.close()
                        ctx.remove_listener("response", _on_ctx_resp)
                        return pdf_text
                body_text = (await popup.locator("body").inner_text(timeout=5000)).strip()
                if body_text and len(body_text) > 100:
                    await popup.close()
                    ctx.remove_listener("response", _on_ctx_resp)
                    return body_text[:50000]
            except Exception as ex:
                print(f"    [OCR] 弹窗 PDF/正文提取失败: {ex}")
            await popup.close()
            ctx.remove_listener("response", _on_ctx_resp)
            return ""

        print(f"    [OCR] Aspose 查看器共 {total_pages} 页")

        # 等第1页响应
        await _wait_for_capture(popup, captured, 1, max_wait=10)

        # 主动翻页拦截剩余页图片
        if total_pages > 1:
            print(f"    [OCR] 开始翻页加载剩余 {total_pages - 1} 页...")
            for pg in range(2, total_pages + 1):
                await _navigate_popup_page(popup, pg)
                await _wait_for_capture(popup, captured, pg, max_wait=8)
                print(f"    [OCR] 第 {pg}/{total_pages} 页 → "
                      f"已拦截 {len(captured)} 张图片")

        # 兜底等一下
        if len(captured) < total_pages:
            await _wait_for_capture(popup, captured, total_pages, max_wait=5)

        # === Fallback：网络拦截失败，改用截图 ===
        if not captured:
            print(f"    [OCR] 网络拦截未捕获图片，切换截图模式...")
            try:
                await popup.evaluate("commonFuncs.goCurrentPage(1)")
                await popup.wait_for_timeout(1500)
            except Exception:
                pass
            captured = await _screenshot_popup_pages(popup, total_pages)

        await popup.close()

    except Exception as ex:
        print(f"    [OCR] 弹窗捕获失败: {ex}")
    finally:
        ctx.remove_listener("response", _on_ctx_resp)

    if captured:
        print(f"    [OCR] 共获取 {len(captured)}/{total_pages} 张图片，开始 OCR...")
        return await _ocr_images(captured)

    inline = await try_read_aspose_viewer_from_page_iframes(page)
    if inline:
        return inline

    return ""


async def extract_aspose_viewer_text(page, html_text: str, viewer_url: str) -> str:
    """
    降级路径：当 HTTP 直接请求返回的是 Aspose 查看器 HTML（非 PDF）时，
    用 Playwright 导航到该 URL，主动翻页 + 截图 OCR。
    """
    m_total = re.search(r'totalPageNum\s*=\s*parseInt\(["\'](\d+)["\']', html_text)
    total_pages = int(m_total.group(1)) if m_total else 4

    captured: list = []
    ctx = page.context

    async def _on_resp(resp):
        url = resp.url
        if "readView" not in url and "gxOpenserviceNews" not in url:
            return
        ct = (resp.headers.get("content-type") or "").lower()
        if "html" in ct or "javascript" in ct:
            return
        try:
            data = await resp.body()
            if data and len(data) > 500:
                captured.append(data)
                print(f"    [Aspose] 拦截到图片 {len(captured)}（{len(data)//1024}KB）")
        except Exception:
            pass

    ctx.on("response", _on_resp)
    viewer_page = await ctx.new_page()
    try:
        await viewer_page.goto(viewer_url, wait_until="domcontentloaded", timeout=25000)
        await _wait_for_capture(viewer_page, captured, 1, max_wait=15)

        for pg in range(2, total_pages + 1):
            await _navigate_popup_page(viewer_page, pg)
            await _wait_for_capture(viewer_page, captured, pg, max_wait=8)
            print(f"    [Aspose] 第 {pg}/{total_pages} 页 → 已拦截 {len(captured)} 张")

        # 截图 fallback
        if not captured:
            print(f"    [Aspose] 网络拦截未捕获图片，切换截图模式...")
            try:
                await viewer_page.evaluate("commonFuncs.goCurrentPage(1)")
                await viewer_page.wait_for_timeout(1500)
            except Exception:
                pass
            captured = await _screenshot_popup_pages(viewer_page, total_pages)
    finally:
        ctx.remove_listener("response", _on_resp)
        await viewer_page.close()

    if captured:
        print(f"    [Aspose] 共获取 {len(captured)}/{total_pages} 张图片，开始 OCR...")
        return await _ocr_images(captured)
    return ""


async def download_and_parse_pdf(page, pdf_url: str) -> str:
    """
    两步策略获取 PDF：
    步骤1：page.request.get() 直接 HTTP 请求（快，适合直链 PDF）
    步骤2：page.goto() 浏览器导航 + 响应拦截（处理 JS 重定向，如 link?target=...）
    """
    if not pdf_url or not _href_is_actionable(pdf_url):
        print(f"    [PDF] 跳过无效或非 HTTP 地址: {(pdf_url or '')[:80]!r}")
        return ""
    # ── 步骤1：HTTP 直接请求 ──────────────────────────────
    try:
        resp = await page.request.get(pdf_url, timeout=30000)
        content_type = (resp.headers.get("content-type") or "").lower()
        if resp.status == 200:
            raw = await resp.body()
            if raw and raw[:4] == b"%PDF":
                return await _parse_pdf_raw(raw)
    except Exception:
        pass

    # ── 步骤2：解析 link?target= 包装，提取真实 URL 直接下载 ──
    # bidcenter 的跳转链接格式：/link?target=<url_encoded_real_url>
    # 直接解码 target 参数，跳过 JS 重定向，直接请求真实地址
    print(f"    [PDF] HTTP 直请求未拿到 PDF，尝试解析 link?target= 包装...")
    try:
        parsed_link = urlparse(pdf_url)
        qs_link = parse_qs(parsed_link.query)
        target_encoded = qs_link.get("target", [""])[0]
        if target_encoded:
            target_url = unquote(target_encoded)
            print(f"    [PDF] 解码真实地址: {target_url}")
            # 模拟浏览器导航型请求头（Sec-Fetch-Mode: navigate）
            _nav_headers = {
                "Referer": "https://www.bidcenter.com.cn/",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;"
                    "q=0.8,application/signed-exchange;v=b3;q=0.7"
                ),
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Site": "cross-site",
                "Upgrade-Insecure-Requests": "1",
            }
            resp2 = await page.request.get(target_url, timeout=30000, headers=_nav_headers)
            if resp2.status == 200:
                raw2 = await resp2.body()
                if raw2 and raw2[:4] == b"%PDF":
                    print(f"    [PDF] 真实地址下载成功")
                    return await _parse_pdf_raw(raw2)
                ct2_type = (resp2.headers.get('content-type') or '')[:60]
                print(f"    [PDF] 真实地址返回非PDF: {ct2_type}")
                # 检测 Aspose 图片查看器（服务器将 PDF 转为逐页图片展示）
                try:
                    html_text2 = raw2.decode("utf-8", errors="replace")
                    if "toHTML-Aspose" in html_text2 or "method=readView" in html_text2:
                        print(f"    [PDF] 检测到 Aspose 图片查看器，启动 OCR 提取...")
                        ocr_result = await extract_aspose_viewer_text(page, html_text2, target_url)
                        if ocr_result:
                            return ocr_result
                except Exception as ex2:
                    print(f"    [PDF] Aspose OCR 失败: {ex2}")
    except Exception as ex:
        print(f"    [PDF] link?target= 解析失败: {ex}")

    # ── 步骤3：expect_download 捕获 PDF 下载文件 ────────────
    # headless Chromium 收到 PDF 通常触发下载而非内嵌显示；
    # accept_downloads=True + expect_download 可捕获下载文件直接读取
    print(f"    [PDF] 尝试捕获下载文件...")
    try:
        async with page.expect_download(timeout=30000) as dl_info:
            await page.goto(pdf_url, wait_until="commit", timeout=30000)
        dl = await dl_info.value
        dl_path = await dl.path()
        if dl_path:
            raw3 = open(dl_path, "rb").read()
            if raw3 and raw3[:4] == b"%PDF":
                print(f"    [PDF] 下载捕获成功：{dl.suggested_filename}")
                return await _parse_pdf_raw(raw3)
    except Exception as ex:
        print(f"    [PDF] 下载捕获失败: {ex}")

    # ── 全部失败：保存 PDF 链接供用户手动访问 ──────────────
    return (
        f"[公告内容为 PDF 文件，自动下载失败]\n"
        f"PDF 地址：{pdf_url}\n"
        f"（请在浏览器中手动打开以上地址查看完整内容）"
    )


def _is_generic_portal_heading(t: str) -> bool:
    """
    Vue 详情页第一个 h1 常为站点品牌（*中国采招网 (bidcenter.com.cn)），
    不能当作公告标题。
    """
    s = (t or "").strip()
    if not s or len(s) < 4:
        return True
    low = s.lower()
    if any(
        k in s
        for k in ("项目", "工程", "采购", "招标", "中标", "成交", "公告", "候选人", "磋商")
    ):
        return False
    if "中国采招网" in s or "bidcenter.com.cn" in low:
        return True
    if s.startswith("*") and "采招" in s:
        return True
    if s in ("中国采招网", "采招网", "首页"):
        return True
    return False


def _strip_doc_title_suffix(raw: str) -> str:
    s = (raw or "").strip()
    # user.bidcenter.com.cn/v2023：页签/DOM 常见「搜索-」「搜索：」「搜索 」+ 标题
    s = re.sub(r"^搜索\s*[-－:：|｜/]+\s*", "", s).strip()
    s = re.sub(r"^搜索\s+", "", s).strip()
    s = re.sub(r"^搜索(?=[\u4e00-\u9fff])", "", s).strip()
    for suf in (
        " - 中国采招网",
        " — 中国采招网",
        "_中国采招网",
        "-中国采招网",
        " | 中国采招网",
        " - 采招网",
    ):
        if suf in s:
            s = s.split(suf)[0].strip()
    return s


def _title_from_vue_news_meta(content: str, url: str) -> str:
    """
    v2023 详情页 inner_text / 正文里常嵌一段 JSON，含当前 news_id 与 news_title_show（列表用语）。
    与「二、项目名称」可能不同，用于区分两条实为同一采购、不同列表标题的记录。
    """
    import json as _json

    m = re.search(r"customDesSearch/(\d+)", (url or ""), re.I)
    if not m:
        return ""
    nid = m.group(1)
    c = content or ""
    anchor = f'"news_id": "{nid}"'
    pos = c.find(anchor)
    if pos < 0:
        anchor2 = f'"news_id": {nid},'
        pos = c.find(anchor2)
    if pos < 0:
        return ""
    chunk = c[pos : pos + 1200]
    m2 = re.search(r'"news_title_show"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
    if not m2:
        return ""
    raw = m2.group(1)
    try:
        t = _json.loads(f'"{raw}"')
    except Exception:
        t = raw.replace("\\\"", '"').replace("\\\\", "\\")
    t = (
        t.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("\\/", "/")
    )
    t = " ".join(t.split()).strip()
    t = _strip_doc_title_suffix(t)
    return t if len(t) > 8 else ""


def _title_from_announcement_body(content: str, info_type: str) -> str:
    """从正文「二、项目名称」或「就…进行…采购」兜底合成标题。"""
    c = (content or "").strip()
    if len(c) < 40:
        return ""
    name = ""
    # Vue 页 inner_text 常为一大段无换行，不能用 .+? 直到 $（会吞掉全文）
    m = re.search(
        r"二、项目名称[：:]\s*(.+?)(?=\s+[一二三四五六七八九十]+、|\n|\r\n|\Z)",
        c,
        re.M,
    )
    if m:
        name = m.group(1).strip()
    if not name or len(name) < 4:
        m2 = re.search(
            r"就\s*(.+?)\s+进行\s*(?:竞争性)?(?:磋商|谈判|招标|采购)",
            c,
            re.M,
        )
        if m2:
            name = m2.group(1).strip()
    if not name or len(name) < 4:
        return ""
    for junk in ("中国采招网", "bidcenter.com.cn", "（?bidcenter", "（bidcenter", ":中国采招网"):
        if junk in name:
            name = name.split(junk)[0].strip()
    name = re.sub(r"[`\*#_]+", "", name).strip()
    name = re.sub(r"[。．\.]+$", "", name).strip()
    name = re.sub(r"[:：\s]+$", "", name).strip()
    name = unwrap_dom_double_glyphs(name, min_len=4)
    # Vue inner_text 有时在汉字间插空格（非双写折叠）
    _prev = None
    while _prev != name:
        _prev = name
        name = re.sub(r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])", r"\1\2", name)
    if not name:
        return ""
    if re.search(r"采购结果[公示公告]", c) or "现将采购结果公示" in c:
        return f"{name}的采购结果公告"
    it = (info_type or "").strip()
    if it:
        if it.endswith("公告"):
            return f"{name}的{it}"
        return f"{name}的{it}公告"
    return f"{name}的公告"


# ─────────────────────────────────────────────────────
# 步骤6：提取详情页内容
# ─────────────────────────────────────────────────────

async def extract_article(page, url: str) -> dict:
    """打开详情页，提取标题/信息类型/正文"""

    # ── PDF 路由拦截：在 goto 之前通过 route 捕获完整 PDF 字节 ──────────────
    # bidcenter 的 PDF 通过 PDF.js viewer 内嵌加载，事后单独 HTTP 请求无法重现。
    # 用 context.route() 拦截 PDF 请求，在响应到达时直接读取完整字节。
    # 附件直链常为 OSS 预签名：https://fileyun.bidcenter.com.cn/bidbsyun/.../xxx.pdf?Expires=...&Signature=...
    # 带 query 的 URL 未必命中 **/*.pdf*，故增加正则 route + response 监听。
    _pdf_bytes_captured: list = []

    async def _intercept_pdf(route, request):
        try:
            if ".pdf" not in (request.url or "").lower():
                await route.continue_()
                return
            resp = await route.fetch()
            ct = (resp.headers.get("content-type") or "").lower()
            body = await resp.body()
            ct_ok = (not ct.strip() or "application/pdf" in ct or "octet-stream" in ct
                     or "pdf" in ct)
            if not _pdf_bytes_captured and body[:4] == b"%PDF" and ct_ok:
                _pdf_bytes_captured.append(body)
                print(f"    [PDF] 路由拦截成功，共 {len(body)} 字节")
            await route.fulfill(response=resp)
        except Exception:
            await route.continue_()

    ctx = page.context
    _fileyun_pdf_re = re.compile(
        r"https://fileyun\.bidcenter\.com\.cn/\S+\.pdf(?:\?|$)", re.I
    )
    await ctx.route("**/*.pdf*", _intercept_pdf)
    await ctx.route(_fileyun_pdf_re, _intercept_pdf)

    async def _on_fileyun_pdf_response(response):
        if _pdf_bytes_captured:
            return
        try:
            u = (response.url or "").lower()
            if "fileyun.bidcenter.com.cn" not in u or ".pdf" not in u:
                return
            if response.status != 200:
                return
            ct = (response.headers.get("content-type") or "").lower()
            # 部分 OSS 回包 Content-Type 为空或非标准，仍以魔数为准
            if ct.strip() and "pdf" not in ct and "octet-stream" not in ct:
                return
            body = await response.body()
            if body and len(body) > 200 and body[:4] == b"%PDF":
                _pdf_bytes_captured.append(body)
                print(f"    [PDF] response 监听捕获 fileyun PDF，共 {len(body)} 字节")
        except Exception:
            pass

    ctx.on("response", _on_fileyun_pdf_response)

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await rand_sleep(page, 800, 2500)    # 详情页渲染随机等待

    # PDF 可能在 iframe 里延迟加载，等一下
    if not _pdf_bytes_captured:
        await page.wait_for_timeout(3000)

    # 标题（避免 Vue 页第一个 h1 为「中国采招网」站点品牌）
    title = ""
    try:
        doc_raw = (await page.title()).strip()
        doc_cut = _strip_doc_title_suffix(doc_raw)
        if (
            doc_cut
            and len(doc_cut) > 8
            and not _is_generic_portal_heading(doc_cut)
            and not _is_generic_portal_heading(doc_raw)
        ):
            title = doc_cut
    except Exception:
        pass
    if not title or _is_generic_portal_heading(title):
        title = ""
        for sel in [
            ".detail-title",
            ".article-title",
            ".announce-title",
            ".des-title",
            ".page-title",
            ".title",
            "h2",
            "h1",
        ]:
            try:
                locs = page.locator(sel)
                n = await locs.count()
                for i in range(min(n, 15)):
                    t = (await locs.nth(i).inner_text(timeout=2000)).strip()
                    t = _strip_doc_title_suffix(" ".join(t.split()))
                    if len(t) > 6 and not _is_generic_portal_heading(t):
                        title = t
                        break
                if title:
                    break
            except Exception:
                continue

    # 信息类型（面包屑最后一级）
    info_type = "采招信息"
    for sel in [".breadcrumb a", ".bread-crumb a", ".crumbs a", ".nav-path a"]:
        try:
            elems = await page.locator(sel).all()
            for e in elems:
                t = (await e.inner_text()).strip()
                if 2 < len(t) < 20:
                    info_type = t
            if info_type != "采招信息":
                break
        except Exception:
            continue

    # 正文（优先用 HTML 转带表格的文本，保留「基本信息」等表格形式）
    content = ""
    content_html = ""
    structured = {"text_blocks": [], "tables": []}
    matched_body_sel: Optional[str] = None
    # 勿用 .el-main / .app-main 等整页布局：会混入侧栏/推荐/站内 JSON，与「只取详情正文」相悖
    for sel in [
        ".article-content", ".detail-content", ".content-body",
        ".main-content", ".news-content", "#content",
        ".bid-content", ".view-content", "article", ".detail",
    ]:
        try:
            loc = page.locator(sel).first
            t = (await loc.inner_text(timeout=2000)).strip()
            if len(t) > 80:
                content = t
                matched_body_sel = sel
                try:
                    raw_html = await loc.inner_html(timeout=2000)
                    content_html = raw_html or ""
                    text_with_tables = html_content_to_text_with_tables(raw_html)
                    if text_with_tables and len(text_with_tables) >= 50:
                        content = text_with_tables
                    structured = html_to_structured_blocks(raw_html)
                except Exception:
                    pass
                break
        except Exception:
            continue

    if not content:
        try:
            content = await page.locator("body").inner_text()
        except Exception:
            content = ""

    if content:
        content = unwrap_dom_double_glyphs(content, min_len=8)

    # PDF 检测触发条件：
    #   1. 正文过短（<80字）
    #   2. 正文含「点击查看或下载公告文件」等 PDF 入口标志
    #      （页面 body 虽然有大量 UI 文字，但 extract_body_below_label
    #       裁剪后只剩这一行，需要在裁剪前就检测并处理 PDF）
    _pdf_hints = [
        "点击查看或下载公告文件", "下载公告文件", "查看公告文件",
        "点击下载", "附件信息可下载", "下载附件", "附件下载",
        "点击查看>>", "点击查看",
        "内容详见附件", "详情请见附件", "详情见附件", "详见附件",
        "详情请点击查看", "点击查看附件", "请点击查看", "点击查看内容",
    ]
    _need_pdf = (
        len(content.strip()) < 80
        or any(h in content for h in _pdf_hints)
        or bool(_pdf_bytes_captured)   # 拦截到 PDF 字节也触发解析
    )

    def _needs_attachment_fallback(cur: str) -> bool:
        """
        是否仍需走附件/PDF/OCR。采招网常见：div 里已有「招标进展」长文案导致 len>80，
        若仍仅用 len<80 判断会跳过 OCR（用户已装 OCR 也抓不到）。
        """
        t = (cur or "").strip()
        if not t:
            return True
        if len(t) < 80:
            return True
        if any(h in t for h in _pdf_hints):
            if len(t) < 1200:
                return True
            if "招标进展" in t:
                return True
        return False

    if _need_pdf:
        # 策略0：优先用页面加载时已拦截的 PDF 字节（最可靠，无需二次请求）
        if _pdf_bytes_captured:
            pdf_text = await _parse_pdf_raw(_pdf_bytes_captured[0])
            if pdf_text and "PDF 无可提取" not in pdf_text:
                content = pdf_text
                print(f"    [PDF] 响应拦截解析完成，共 {len(pdf_text)} 字")
        # 策略1：先弹窗 / 页内查看器 + OCR（多数「详见附件」走 window.open 或 iframe，不是直下载）
        if _needs_attachment_fallback(content):
            ocr_text = await ocr_via_popup_click(page)
            if ocr_text:
                content = ocr_text
                print(f"    [OCR] popup/iframe OCR 完成，共 {len(ocr_text)} 字")
        # 策略2：直链下载 PDF（expect_download；放在 OCR 后避免误点下载导致弹窗逻辑失效）
        if _needs_attachment_fallback(content):
            raw_dl = await try_download_attachment_bytes(page)
            if raw_dl:
                pdf_text = await _parse_pdf_raw(raw_dl)
                if pdf_text and "PDF 无可提取" not in pdf_text and len(pdf_text.strip()) >= 40:
                    content = pdf_text
                    print(f"    [PDF] 下载捕获解析完成，共 {len(pdf_text)} 字")
        # 策略3：解析页面中的 PDF URL
        if _needs_attachment_fallback(content):
            pdf_url = await find_pdf_url(page)
            if pdf_url:
                print(f"    [PDF] 检测到 PDF，尝试解析: {pdf_url}")
                pdf_text = await download_and_parse_pdf(page, pdf_url)
                if pdf_text:
                    content = pdf_text
                    print(f"    [PDF] 解析完成，共 {len(pdf_text)} 字")
    if not content:
        content = "无法提取内容"

    # 详情页 inner_text 可能读到「每字双写」的 DOM（与列表页同源问题）
    if title:
        title = unwrap_dom_double_glyphs(title.strip(), min_len=6)
    if content and content != "无法提取内容":
        content = unwrap_dom_double_glyphs(content, min_len=8)

    # 正文区 <img> OCR：可配置关闭（附件 PDF / 弹窗 Aspose 等 OCR 仍走上文 _need_pdf 分支）
    if (
        ARTICLE_INLINE_IMAGE_OCR
        and content
        and content != "无法提取内容"
    ):
        try:
            img_ocr = await ocr_inline_images_from_article_html(
                page,
                content_html or "",
                primary_selector=matched_body_sel,
            )
            if img_ocr and not img_ocr.strip().startswith("[OCR 不可用]"):
                placed = False
                for mk in ("四、成交内容：", "四、成交内容:"):
                    if mk in content:
                        content = content.replace(
                            mk, mk + "\n\n[附图OCR]\n" + img_ocr, 1
                        )
                        placed = True
                        break
                if not placed:
                    content = (
                        content
                        + "\n\n[以下为正文中嵌入图片的自动识别，供检索与入库；可能有错漏]\n"
                        + img_ocr
                    )
        except Exception as ex:
            print(f"    [配图OCR] 跳过: {ex}")

    # 无 <table> 时 inner_text 常为竖排表头+竖排数据，尽量还原为制表符行供前端表格渲染
    if content and content != "无法提取内容":
        content, extra_repaired = repair_vertical_list_tables_in_plaintext(
            content, structured.get("tables", [])
        )
        if extra_repaired:
            structured["tables"] = list(structured.get("tables", [])) + extra_repaired
        content, extra_wh = repair_weihai_style_review_result_table(
            content, structured.get("tables", [])
        )
        if extra_wh:
            structured["tables"] = list(structured.get("tables", [])) + extra_wh
        content, extra_wz = repair_zhongbiao_result_vertical_supplier_table(
            content, structured.get("tables", [])
        )
        if extra_wz:
            structured["tables"] = list(structured.get("tables", [])) + extra_wz
        content, extra_nb = repair_chengjiao_content_vertical_four_col_table(
            content, structured.get("tables", [])
        )
        if extra_nb:
            structured["tables"] = list(structured.get("tables", [])) + extra_nb
        content, extra_sz = repair_shouzheng_xunyuan_vertical_three_col_table(
            content, structured.get("tables", [])
        )
        if extra_sz:
            structured["tables"] = list(structured.get("tables", [])) + extra_sz

    # 站点型 / 路由型标题纠正：用正文「二、项目名称」等合成真实公告名
    t_meta = _title_from_vue_news_meta(content, url)
    t_body = _title_from_announcement_body(content, info_type)
    ulow = (url or "").lower()
    if "/v2023/" in ulow and "bidcenter.com.cn" in ulow:
        if t_meta and len(t_meta) > 10:
            title = _strip_doc_title_suffix(t_meta)
        elif t_body:
            title = _strip_doc_title_suffix(t_body)
    elif t_body and (
        _is_generic_portal_heading(title) or len((title or "").strip()) < 6
    ):
        title = _strip_doc_title_suffix(t_body)

    title = _strip_doc_title_suffix((title or "").strip())

    # 标题已用完整正文推断后，再裁剪为「详情正文/公告正文」以下，与入库/JSON 一致
    if content and content != "无法提取内容":
        content = extract_body_below_label(content)
    structured["text_blocks"] = text_blocks_from_detail_body(
        content if content and content != "无法提取内容" else ""
    )

    await ctx.unroute("**/*.pdf*", _intercept_pdf)
    await ctx.unroute(_fileyun_pdf_re, _intercept_pdf)
    try:
        ctx.remove_listener("response", _on_fileyun_pdf_response)
    except Exception:
        pass

    return {
        "url":          url,
        "title":        title or "无标题",
        "info_type":    info_type,
        "content":      content,
        "content_html": content_html,
        "text_blocks":  structured.get("text_blocks", []),
        "tables":       structured.get("tables", []),
    }


# ─────────────────────────────────────────────────────
# 单关键词完整爬取
# ─────────────────────────────────────────────────────

async def scrape_keyword(keyword: str, context, out_dir: str, max_pages: int,
                         start_date: str = "", end_date: str = ""):
    """
    完整流程：
    1. 主站搜索框 → jq_search() → 新标签页
    2. 点击「标题搜索」
    3. 逐页解析结果 → 每条开新标签页提取内容 → 保存文件
    """
    print(f"\n{'─'*52}")
    print(f"  关键词：「{keyword}」  输出目录：{out_dir}/")

    # 2. 打开搜索结果标签页
    search_page = await open_search_tab(context, keyword)

    # 3. 点击「标题搜索」
    await click_title_search(search_page)

    # 3.5 应用时间筛选
    await apply_date_filter(search_page, start_date, end_date)

    # 3.6 应用搜索模式
    await apply_search_mode(search_page, SEARCH_MODE)
    # 结果列表常为 JS 异步加载，多等一会再解析
    await rand_sleep(search_page, 2000, 4000)

    saved_count  = 0
    failed_count = 0
    seen_urls_this_keyword = set()  # 本关键词下已处理过的 URL，避免同项目跨页/同页重复

    for page_no in range(1, max_pages + 1):
        print(f"\n  [第 {page_no} 页]")

        # 4. 解析当前页列表
        links = await get_result_links(search_page)
        if not links:
            print("  无结果，停止翻页。")
            break

        # 本页链接若全部在之前页已处理过，说明是重复页（如实际只有4页但翻页仍返回旧内容），停止翻页
        new_on_page = [item for item in links if item["url"] not in seen_urls_this_keyword]
        if not new_on_page:
            print("  本页链接均已在之前页处理过，视为重复页，停止翻页。")
            break

        # 5. 逐条打开详情
        for idx, item in enumerate(links, 1):
            url       = item["url"]
            title     = item["title"]
            info_type = item["info_type"]

            # 本关键词下已处理过该 URL 则跳过（同项目多页或列表重复只保存一次）
            if url in seen_urls_this_keyword:
                print(f"\n  [{idx}/{len(links)}] [SKIP] 重复项目（已处理）→ {title[:45]}")
                continue
            seen_urls_this_keyword.add(url)

            title_short = (title[:45] + "…") if len(title) > 45 else title
            print(f"\n  [{idx}/{len(links)}] 标题: {title_short}")

            # 先按标题判断是否与科技相关（监管报送/IT系统建设/软件/其它科技），非科技则跳过，不打开详情页
            is_tech = is_tech_related_by_title(title) if is_tech_related_by_title is not None else True
            print(f"      [科技识别] 标题是否科技相关: {'是' if is_tech else '否'}")
            if is_tech_related_by_title is not None and not is_tech:
                print(f"      [SKIP] 标题非科技相关，不进入详情爬取")
                continue

            # 标题排除过滤：命中任意排除词则跳过，不打开详情页
            hit = next((kw for kw in EXCLUDE_KEYWORDS if kw in title), None)
            if hit:
                print(f"      [过滤-排除词] 命中「{hit}」→ 跳过")
                continue
            print(f"      [过滤-排除词] 未命中，通过")

            # 标题须包含当前搜索关键词，否则跳过（避免无关结果）
            if keyword not in title:
                print(f"      [过滤-关键词] 标题不包含「{keyword}」→ 跳过")
                continue
            print(f"      [过滤-关键词] 包含「{keyword}」，通过")

            print(f"      [OK] 通过全部筛选，进入详情爬取")

            detail = await context.new_page()
            try:
                article = await extract_article(detail, url)
                # 类型统一取自「查询结果列表前的类型标签」（如招标公告），不再用详情页面包屑，避免类型过多
                final_type = info_type
                save_title = title if title else article["title"]
                body_content = extract_body_below_label(article["content"])
                body_html = _strip_html_after_recommend(article.get("content_html") or "")
                if OUTPUT_FORMAT in ("txt", "both"):
                    save_to_file(final_type, save_title, url, body_content, out_dir)
                if OUTPUT_FORMAT in ("html", "both"):
                    save_to_html(final_type, save_title, url, body_html, out_dir, text_fallback=body_content)
                if OUTPUT_FORMAT in ("json", "both"):
                    save_to_json(final_type, save_title, url,
                                 article.get("text_blocks", []),
                                 article.get("tables", []),
                                 body_content, out_dir)
                saved_count += 1
                # 单条不再推送企业微信，由 pipeline 统一发「今日摘要」公众号式卡片
            except Exception as e:
                print(f"    [FAIL] {e}")
                failed_count += 1
            finally:
                await detail.close()

            # 每条详情页抓取完后随机停顿，模拟真人浏览间隔
            await rand_sleep(search_page, 1000, 4000)

        # 6. 翻页
        if page_no < max_pages:
            has_next = await go_next_page(search_page)
            if not has_next:
                print("\n  没有下一页，本关键词爬取完毕。")
                break

    print(f"  [OK] 「{keyword}」爬取结束，正在关闭标签页…", flush=True)
    try:
        await asyncio.wait_for(search_page.close(), timeout=15.0)
    except asyncio.TimeoutError:
        print("  [WARN] 关闭标签页超时(15s)，继续执行。", flush=True)
    except Exception as e:
        print(f"  [WARN] 关闭标签页异常: {e}", flush=True)
    print(f"\n  「{keyword}」完成：成功 {saved_count} 篇，失败 {failed_count} 篇")
    return saved_count, failed_count


async def _run_blocking_call(fn, *args):
    """在后台线程执行同步函数（AI+MySQL），避免阻塞 Playwright 事件循环。"""
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(fn, *args)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))


async def recrawl_pending_audit_urls(context, items: list, config_path: Path) -> tuple[int, int]:
    """
    已登录状态下，按库中 detail_url 直开详情 → 正文经 AI 分析 → 按 detail_url UPDATE 库（不写 output JSON）。
    items: [{"detail_url","keyword","sub_type","title"}, ...]
    """
    if not items:
        return 0, 0
    from pending_reaudit_ingest import sync_update_row_from_extract

    print(f"\n{'='*52}")
    print(f"  待审核补爬：共 {len(items)} 个 URL（直开详情 → AI → 更新库，不落盘 JSON）")
    print(f"  配置：{config_path}")
    print(f"{'='*52}")
    ok_count = 0
    failed_count = 0
    for idx, item in enumerate(items, 1):
        url = (item.get("detail_url") or "").strip()
        if not url:
            continue
        kw = (item.get("keyword") or "银行").strip() or "银行"
        info_type = (item.get("sub_type") or "采招信息").strip() or "采招信息"
        title_fb = (item.get("title") or "").strip()
        print(f"\n  [待审核补爬 {idx}/{len(items)}] {url[:80]}…")
        detail = await context.new_page()
        try:
            article = await extract_article(detail, url)
            body_content = extract_body_below_label(article["content"])
            ok, msg = await _run_blocking_call(
                sync_update_row_from_extract,
                config_path,
                url,
                kw,
                info_type,
                title_fb,
                article,
                body_content,
            )
            if ok:
                ok_count += 1
                print(f"    [OK] 库已更新: {msg}")
            else:
                failed_count += 1
                print(f"    [FAIL] {msg}")
        except Exception as e:
            print(f"    [FAIL] {e}")
            failed_count += 1
        finally:
            await detail.close()
        await rand_sleep(None, 1000, 4000)
    print(f"\n  [待审核补爬] 完成：库更新成功 {ok_count} 条，失败 {failed_count} 条")
    return ok_count, failed_count


# ─────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────

async def scrape(
    keywords: list[str],
    max_pages: int = MAX_PAGES,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
    pending_reurls=None,
    pending_reaudit_config=None,
):
    date_range = f"{start_date} ~ {end_date}" if (start_date or end_date) else "不限"
    print(f"\n{'='*52}")
    print(f"  采招网信息爬取工具（标题搜索）")
    print(f"  关键词：{keywords}")
    print(f"  时间范围：{date_range}")
    print(f"  每词最多 {max_pages} 页  |  输出格式：{OUTPUT_FORMAT}  |  根目录：{OUTPUT_DIR}/")
    print(f"{'='*52}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,   # 允许捕获 PDF 下载
        )
        main_page = await context.new_page()

        try:
            # 1. 只登录一次
            await login(main_page)
            await main_page.close()  # 登录完就不需要了

            total_saved  = 0
            total_failed = 0

            # 文件夹名以 config.ini start_date 为准；留空则用运行当天
            date_str = start_date if start_date else datetime.now().strftime("%Y-%m-%d")
            base_out = os.path.join(OUTPUT_DIR, date_str)

            if pending_reurls:
                cfgp = pending_reaudit_config or _CONFIG_FILE
                pu, pf = await recrawl_pending_audit_urls(context, pending_reurls, cfgp)
                total_saved += pu
                total_failed += pf

            for i, kw in enumerate(keywords, 1):
                print(f"\n{'='*52}")
                print(f"  [{i}/{len(keywords)}] 关键词：「{kw}」")
                kw_dir = os.path.join(base_out, sanitize_filename(kw))
                saved, failed = await scrape_keyword(kw, context, kw_dir, max_pages,
                                                     start_date, end_date)
                total_saved  += saved
                total_failed += failed
                # 关键词之间随机停顿（不用已关闭的标签页，避免卡住）
                if i < len(keywords):
                    print(f"  [OK] 等待 2~6 秒后继续下一关键词…", flush=True)
                    await asyncio.sleep(random.randint(2000, 6000) / 1000.0)

            print(f"\n  [OK] 全部关键词已处理完毕。", flush=True)
            print(f"\n{'='*52}")
            print(f"  [完成] 全部关键词爬取结束！")
            print(f"  关键词数：{len(keywords)} 个")
            print(f"  成功保存：{total_saved} 篇")
            print(f"  失败跳过：{total_failed} 篇")
            print(f"  输出根目录：{os.path.abspath(base_out)}/  （{OUTPUT_DIR}/{date_str}/）")
            print(f"{'='*52}\n")
            return {
                "total_saved": total_saved,
                "total_failed": total_failed,
                "date_str": date_str,
                "base_out": base_out,
                "keywords": list(keywords),
            }

        except Exception as exc:
            print(f"\n  [ERROR] {exc}")
            try:
                p = context.pages[-1] if context.pages else None
                if p:
                    await safe_screenshot(p, "error_snapshot")
            except Exception:
                pass
            raise
        finally:
            await browser.close()


# ─────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        raw_args = fix_argv_encoding()
        keywords = [k.strip() for k in re.split(r"[,，]", " ".join(raw_args)) if k.strip()]
    else:
        keywords = _CFG["keywords"]

    if not keywords:
        keywords = ["招标公告"]

    print(f"将依次爬取 {len(keywords)} 个关键词：{keywords}")
    asyncio.run(scrape(keywords, _CFG["max_pages"]))
