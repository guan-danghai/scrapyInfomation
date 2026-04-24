#!/usr/bin/env python3
"""
从保存的招标公告 HTML 页面（Aspose PDF 预览器）中提取招投标信息。

原理：
  HTML 页面是一个 PDF 在线预览器，PDF 的每一页被转成了图片。
  本程序通过以下步骤提取信息：
  1. 解析 HTML，获取页面总数和已保存的图片引用
  2. 对每张图片使用 EasyOCR 做中文 OCR 识别
  3. 将识别出的文字拼接，再用正则提取结构化的招投标字段
  4. 输出结构化 JSON 和纯文本

用法：
  python parse_bid_html.py <html_file_path>
  python parse_bid_html.py   （默认读取 pdf 目录下第一个 .html）
"""

import sys
import os
import re
import json
import glob
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ─── HTML 解析：提取图片路径和元数据 ───

def parse_html_meta(html_path: str) -> dict:
    """从 HTML 文件中提取 PDF 预览器的元数据。"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    total_pages = 1
    m = re.search(r'var\s+totalPageNum\s*=\s*parseInt\("(\d+)"\)', content)
    if m:
        total_pages = int(m.group(1))

    fd_id = ""
    m = re.search(r'var\s+fdId\s*=\s*"([^"]+)"', content)
    if m:
        fd_id = m.group(1)

    title = ""
    m = re.search(r"<title>\s*(.*?)\s*</title>", content, re.DOTALL)
    if m:
        title = m.group(1).strip()

    img_srcs = re.findall(r'<img[^>]+id="dataLoad_\d+"[^>]+src="([^"]+)"', content)

    return {
        "total_pages": total_pages,
        "fd_id": fd_id,
        "title": title,
        "img_srcs": img_srcs,
        "html_dir": os.path.dirname(os.path.abspath(html_path)),
    }


def find_local_images(html_path: str, meta: dict) -> list[str]:
    """在 HTML 的 _files 目录中查找已保存的页面图片。"""
    html_abs = os.path.abspath(html_path)
    base_name = os.path.splitext(os.path.basename(html_abs))[0]
    files_dir = os.path.join(os.path.dirname(html_abs), f"{base_name}_files")

    found = []
    for src in meta["img_srcs"]:
        img_name = src.split("/")[-1]
        full_path = os.path.join(files_dir, img_name)
        if os.path.isfile(full_path):
            found.append(full_path)

    if not found:
        for ext in ("*.do", "*.png", "*.jpg", "*.jpeg", "*.bmp"):
            for f in glob.glob(os.path.join(files_dir, ext)):
                fsize = os.path.getsize(f)
                if fsize > 10000 and "pdf.png" not in f and "logo" not in f:
                    found.append(f)

    return found


# ─── OCR 文字识别 ───

def _ensure_readable_image(img_path: str) -> str:
    """确保图片文件可被 OpenCV/Pillow 读取。
    如果扩展名非标准（如 .do），则根据文件头检测格式并复制为正确扩展名。
    """
    from PIL import Image
    import shutil
    import tempfile

    known_exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
    ext = os.path.splitext(img_path)[1].lower()
    if ext in known_exts:
        return img_path

    try:
        img = Image.open(img_path)
        fmt = img.format or "PNG"
        ext_map = {"JPEG": ".jpg", "PNG": ".png", "BMP": ".bmp", "GIF": ".gif",
                   "TIFF": ".tiff", "WEBP": ".webp"}
        new_ext = ext_map.get(fmt.upper(), ".png")
        tmp_dir = os.path.join(tempfile.gettempdir(), "bid_ocr_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        new_path = os.path.join(tmp_dir, os.path.basename(img_path) + new_ext)
        img.save(new_path)
        print(f"    图片格式: {fmt}，已转换为 {new_ext}")
        return new_path
    except Exception as e:
        print(f"    ⚠ 无法识别图片格式: {e}")
        return img_path


def ocr_images(image_paths: list[str]) -> str:
    """对图片列表做 OCR（使用 RapidOCR），返回合并后的全文文本。"""
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image
    import numpy as np

    print(f"  正在初始化 RapidOCR...")
    engine = RapidOCR()

    all_text = []
    for i, img_path in enumerate(image_paths, 1):
        print(f"  OCR 识别第 {i}/{len(image_paths)} 页: {os.path.basename(img_path)}")
        readable_path = _ensure_readable_image(img_path)
        img = Image.open(readable_path).convert("RGB")
        img_np = np.array(img)
        result, elapse = engine(img_np)
        if result:
            page_text = "\n".join(line[1] for line in result)
        else:
            page_text = "(未识别到文字)"
        all_text.append(f"===== 第 {i} 页 =====\n{page_text}")

    return "\n\n".join(all_text)


# ─── 结构化信息提取 ───

def _join_ocr_lines(text: str) -> str:
    """将 OCR 按图片行边界断开的中文文本重新拼接。
    规则：如果当前行末尾是中文字符且下一行开头也是中文字符，去掉换行拼接。
    """
    lines = text.split("\n")
    if len(lines) <= 1:
        return text
    merged = [lines[0]]
    for line in lines[1:]:
        if line.startswith("====="):
            merged.append(line)
            continue
        prev = merged[-1]
        if prev and line:
            last_char = prev[-1]
            first_char = line[0]
            _CN_END = set("，。、；：）》】\u201d\u2019")
            _CN_START = set("（《【\u201c\u2018")
            is_cn = lambda c: '\u4e00' <= c <= '\u9fff' or c in _CN_END
            is_cn_start = lambda c: '\u4e00' <= c <= '\u9fff' or c in _CN_START
            if (is_cn(last_char) and is_cn_start(first_char)) or \
               (is_cn(last_char) and first_char == '(') or \
               (last_char in '，、' and is_cn_start(first_char)):
                merged[-1] = prev + line
                continue
        merged.append(line)
    return "\n".join(merged)


FIELD_PATTERNS = [
    ("项目名称", [
        r"项\s*目\s*名\s*称[：:]\s*(.+?)(?:[。\n]|$)",
        r"1\s*[\.、]\s*项目名称[：:]\s*(.+?)(?:[。\n]|$)",
    ]),
    ("招标编号", [
        r"招标编号[：:]\s*([A-Za-z0-9\-]+)",
        r"编\s*号[：:]\s*([A-Za-z0-9\-]+)",
        r"(GXTC-[A-Za-z]-\d+)",
    ]),
    ("发布日期", [
        r"日\s*期[：:]\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日且])",
        r"发布[日时]期[：:]\s*(.+?)(?:\n|$)",
    ]),
    ("项目预算", [
        r"项目预算\s*(\d+[\d\.]*\s*万元)",
        r"预算[：:]\s*(.+?万元)",
        r"预算\s*(\d+[\d\.]*\s*万元)",
    ]),
    ("资金来源", [
        r"资金来源[：:]\s*(.+?)(?=[，。,])",
        r"项目资金来源[：:]\s*(.+?)(?=[，。,])",
    ]),
    ("招标人/采购人", [
        r"受(.+?)[（(]以",
        r"招\s*标\s*人[（(].*?[）)]\s*[：:]?\s*(.+?)(?=[，。,\n]|$)",
        r"采\s*购\s*人[：:]\s*(.+?)(?=[，。,\n]|$)",
    ]),
    ("代理机构", [
        r"(.+?)[（(]招标代理机构[）)]",
        r"代理机构[：:]\s*(.+?)(?=[，。,\n]|$)",
        r"招标代理[：:]\s*(.+?)(?=[，。,\n]|$)",
    ]),
    ("投标截止时间", [
        r"投标截止[时日]间[：:]\s*(.+?)(?:\n|$)",
        r"截止[时日][间期][：:]\s*(.+?)(?:\n|$)",
        r"递交投标文件的截止时间[：:为]\s*(.+?)(?:[。\n]|$)",
        r"截止时间[为：:]\s*(.+?)(?=[。\n，,]|$)",
    ]),
    ("开标时间", [
        r"开标时间[为：:]\s*(.+?)(?=[。\n，,]|$)",
    ]),
    ("开标地点", [
        r"开标地[点址][为：:]\s*(.+?)(?=[。\n，,]|$)",
    ]),
    ("联系人", [
        r"联\s*系\s*人[：:]\s*(.+?)(?:\n|$)",
        r"项目联系人[：:]\s*(.+?)(?:\n|$)",
    ]),
    ("联系电话", [
        r"联系电话[：:]\s*([\d\-]+)",
        r"电\s*话[：:]\s*([\d\-]+)",
        r"电话[为：:]\s*([\d\-]+)",
    ]),
    ("项目概述", [
        r"项目概[述说][：:]\s*(.+?)(?=[。\n])",
        r"3\s*[\.、]\s*项目概[述说][：:]\s*(.+?)(?=[。\n])",
    ]),
]


def _post_process_field(name: str, value: str) -> str:
    """对特定字段做 OCR 纠错后处理。"""
    if "日期" in name or "时间" in name:
        value = re.sub(r"(\d{1,2})\s*且", r"\1日", value)
        value = re.sub(r"(\d{1,2})\s*曰", r"\1日", value)
    return value


def extract_fields(text: str) -> dict:
    """从 OCR 文本中提取结构化招投标字段。"""
    merged_text = _join_ocr_lines(text)
    result = {}
    for field_name, patterns in FIELD_PATTERNS:
        for pat in patterns:
            m = re.search(pat, merged_text, re.MULTILINE)
            if m:
                val = m.group(1).strip()
                val = re.sub(r"\s+", " ", val)
                val = _post_process_field(field_name, val)
                result[field_name] = val
                break
    return result


def extract_full_sections(text: str) -> dict:
    """提取完整的段落内容（资格要求等长段落）。"""
    merged_text = _join_ocr_lines(text)
    sections = {}

    qual_match = re.search(
        r"(合格投标人的?资格要求|资格要求)[：:]?\s*([\s\S]*?)(?=\d+\s*[\.、]\s*(?:投标|招标|其他)|$)",
        merged_text
    )
    if qual_match:
        sections["资格要求"] = qual_match.group(2).strip()[:3000]

    return sections


# ─── 主流程 ───

def main():
    if len(sys.argv) > 1:
        html_path = sys.argv[1]
    else:
        pdf_dir = os.path.join(os.path.dirname(__file__), "pdf")
        htmls = glob.glob(os.path.join(pdf_dir, "*.html"))
        if not htmls:
            print("错误：未找到 HTML 文件。请指定文件路径作为参数。")
            sys.exit(1)
        html_path = htmls[0]

    html_path = os.path.abspath(html_path)
    print(f"╔══════════════════════════════════════════╗")
    print(f"║   招标公告 HTML 解析工具                 ║")
    print(f"╚══════════════════════════════════════════╝")
    print(f"\n目标文件: {html_path}")

    if not os.path.isfile(html_path):
        print(f"错误：文件不存在 → {html_path}")
        sys.exit(1)

    # 步骤1: 解析 HTML 元数据
    print("\n[1/4] 解析 HTML 元数据...")
    meta = parse_html_meta(html_path)
    print(f"  文档标题: {meta['title']}")
    print(f"  总页数:   {meta['total_pages']}")
    print(f"  文档ID:   {meta['fd_id']}")
    print(f"  已找到图片引用: {len(meta['img_srcs'])} 个")

    # 步骤2: 查找本地图片
    print("\n[2/4] 查找本地已保存的页面图片...")
    images = find_local_images(html_path, meta)
    if not images:
        print("  ⚠ 未找到任何页面图片！")
        print("  提示：请确保 HTML 的 _files 目录中包含页面图片文件。")
        sys.exit(1)

    print(f"  找到 {len(images)} 张图片（共 {meta['total_pages']} 页）:")
    for img in images:
        fsize = os.path.getsize(img) / 1024
        print(f"    - {os.path.basename(img)} ({fsize:.1f} KB)")

    if len(images) < meta["total_pages"]:
        print(f"  ⚠ 注意：仅有 {len(images)}/{meta['total_pages']} 页的图片已保存到本地。")
        print(f"    其余页面的图片需要从服务器动态加载，本地无法获取。")

    # 步骤3: OCR 识别
    print("\n[3/4] OCR 文字识别...")
    ocr_text = ocr_images(images)

    # 步骤4: 提取结构化信息
    print("\n[4/4] 提取招投标信息...")
    fields = extract_fields(ocr_text)
    sections = extract_full_sections(ocr_text)

    # 输出结果
    print("\n" + "=" * 60)
    print("  提取结果")
    print("=" * 60)

    if fields:
        for k, v in fields.items():
            print(f"  {k}: {v}")
    else:
        print("  未提取到结构化字段（OCR 文本可能需要人工检查）")

    if sections:
        print("\n--- 详细段落 ---")
        for k, v in sections.items():
            print(f"\n  [{k}]")
            for line in v.split("\n"):
                print(f"    {line}")

    # 保存结果
    out_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(out_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", meta["title"])[:80] or "bid_info"

    # JSON 格式
    json_path = os.path.join(out_dir, f"{safe_title}_{timestamp}.json")
    output_data = {
        "meta": {
            "source_file": html_path,
            "title": meta["title"],
            "total_pages": meta["total_pages"],
            "pages_parsed": len(images),
            "parse_time": datetime.now().isoformat(),
        },
        "fields": fields,
        "sections": sections,
        "ocr_full_text": ocr_text,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print(f"\n  JSON 已保存: {json_path}")

    # 纯文本格式
    txt_path = os.path.join(out_dir, f"{safe_title}_{timestamp}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"标题: {meta['title']}\n")
        f.write(f"来源: {html_path}\n")
        f.write(f"解析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"总页数: {meta['total_pages']}  已解析: {len(images)} 页\n")
        f.write("=" * 60 + "\n\n")
        for k, v in fields.items():
            f.write(f"{k}: {v}\n")
        if sections:
            f.write("\n" + "-" * 40 + "\n")
            for k, v in sections.items():
                f.write(f"\n[{k}]\n{v}\n")
        f.write("\n\n" + "=" * 60 + "\n")
        f.write("OCR 原始文本:\n")
        f.write(ocr_text)
    print(f"  TXT 已保存: {txt_path}")

    print("\n完成！")


if __name__ == "__main__":
    main()
