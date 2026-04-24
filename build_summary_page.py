#!/usr/bin/env python3
"""
将指定 output 子目录（如「银行」）下所有 JSON 采招结果生成一个汇总 HTML 页面，
并可选择以「公众号式」文本卡片推送到企业微信。

用法：
  python build_summary_page.py
  python build_summary_page.py 银行
  python build_summary_page.py 银行 --no-push   # 只生成页面，不推送

依赖：output 子目录下为 scraper 保存的 .json 文件（含 title, info_type, url, crawl_time）。
"""

import json
import sys
from pathlib import Path
import configparser

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def get_output_dir(cfg: configparser.ConfigParser) -> Path:
    out = cfg["scraper"].get("output_dir", "output").strip() if cfg.has_section("scraper") else "output"
    return ROOT / out


def get_default_keyword(cfg: configparser.ConfigParser) -> str:
    if cfg.has_section("scraper") and cfg["scraper"].get("keywords"):
        first = cfg["scraper"].get("keywords", "").strip().split(",")[0].strip()
        if first:
            return first
    return "银行"


def load_items_from_jsons(dir_path: Path) -> list[dict]:
    """从目录下所有 .json 读取条目，按 (title, url) 去重，返回列表，每项含 title, info_type, url, crawl_time。"""
    seen = set()
    items = []
    for f in sorted(dir_path.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  跳过（解析失败）: {f.name} -> {e}")
            continue
        title = (data.get("title") or "").strip()
        url = (data.get("url") or "").strip()
        if not title and not url:
            continue
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title,
            "info_type": (data.get("info_type") or "采招信息").strip(),
            "url": url,
            "crawl_time": (data.get("crawl_time") or "").strip(),
        })
    # 按 crawl_time 倒序，无时间的排后面
    def sort_key(x):
        t = x.get("crawl_time") or ""
        return (0 if t else 1, t)

    items.sort(key=sort_key, reverse=True)
    return items


def build_html(keyword: str, items: list[dict], output_path: Path) -> None:
    """生成暗色主题、适合阅读的汇总 HTML。"""
    rows = []
    for i, it in enumerate(items, 1):
        title = it["title"]
        url = it["url"]
        info_type = it["info_type"]
        crawl_time = it["crawl_time"]
        safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        rows.append(
            f'<tr><td class="idx">{i}</td>'
            f'<td class="type">{info_type}</td>'
            f'<td class="time">{crawl_time}</td>'
            f'<td class="title"><a href="{url}" target="_blank" rel="noopener">{safe_title}</a></td></tr>'
        )
    table_body = "\n".join(rows)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{keyword} 采招汇总</title>
  <style>
    :root {{ --bg: #1a1a1e; --card: #25252b; --text: #e4e4e7; --muted: #9ca3af; --accent: #60a5fa; --border: #3f3f46; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 1rem; font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
    h1 {{ font-size: 1.25rem; color: var(--text); margin: 0 0 1rem 0; }}
    .meta {{ color: var(--muted); font-size: 0.875rem; margin-bottom: 1rem; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); border-radius: 8px; overflow: hidden; }}
    th {{ text-align: left; padding: 0.75rem 1rem; background: var(--border); color: var(--muted); font-weight: 600; font-size: 0.75rem; }}
    td {{ padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); }}
    tr:last-child td {{ border-bottom: none; }}
    td.idx {{ width: 3rem; color: var(--muted); }}
    td.type {{ width: 6rem; color: var(--muted); font-size: 0.875rem; }}
    td.time {{ width: 11rem; color: var(--muted); font-size: 0.875rem; }}
    td.title {{ }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>{keyword} 采招汇总</h1>
  <p class="meta">共 {len(items)} 条，点击标题可打开原文链接。</p>
  <table>
    <thead><tr><th>#</th><th>类型</th><th>时间</th><th>标题</th></tr></thead>
    <tbody>
{table_body}
    </tbody>
  </table>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"已生成: {output_path}")


def push_summary_to_wecom(
    keyword: str,
    total_count: int,
    page_url: str,
    cfg: configparser.ConfigParser,
    do_push: bool,
) -> bool:
    """若 do_push 且 [wecom] 配置了应用消息与 summary_base_url，则发送 textcard。"""
    if not do_push:
        return False
    if not cfg.has_section("wecom"):
        return False
    w = cfg["wecom"]
    corp_id = w.get("corp_id", "").strip()
    agent_id = w.get("agent_id", "").strip()
    secret = w.get("secret", "").strip()
    to_user = w.get("to_user", "").strip()
    to_chatid = w.get("to_chatid", "").strip()
    if not (corp_id and agent_id and secret and (to_user or to_chatid)):
        return False
    if not page_url or not page_url.startswith("http"):
        print("未配置可访问的汇总页地址（summary_base_url 或 summary_page_url），跳过推送。")
        print("请将生成的 HTML 部署到可访问的 URL，并在 config.ini [wecom] 中配置后重新运行。")
        return False

    try:
        import wecom_notify
    except ImportError:
        print("未找到 wecom_notify 模块，跳过推送。")
        return False

    token = wecom_notify.get_access_token(corp_id, secret)
    if not token:
        print("获取企业微信 token 失败，跳过推送。")
        return False

    title = f"{keyword} 采招汇总"
    description = f"共 {total_count} 条，点击查看完整列表。"
    ok = wecom_notify.send_app_textcard(
        access_token=token,
        agent_id=int(agent_id) if agent_id.isdigit() else agent_id,
        title=title,
        description=description,
        url=page_url,
        btntxt="查看",
        touser=to_user or None,
        chatid=to_chatid or None,
    )
    if ok:
        print("已以「文本卡片」形式推送到企业微信。")
    return ok


def main() -> None:
    cfg = load_config()
    output_base = get_output_dir(cfg)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_push = "--no-push" not in sys.argv
    keyword = args[0].strip() if args else get_default_keyword(cfg)

    dir_path = output_base / keyword
    if not dir_path.is_dir():
        print(f"错误：目录不存在 -> {dir_path}")
        sys.exit(1)

    items = load_items_from_jsons(dir_path)
    if not items:
        print(f"该目录下没有有效 JSON 文件 -> {dir_path}")
        sys.exit(0)

    summary_name = f"汇总_{keyword}.html"
    output_path = dir_path / summary_name
    build_html(keyword, items, output_path)
    print(f"共 {len(items)} 条。")

    # 汇总页可访问 URL：优先 summary_page_url，否则 summary_base_url + 相对路径
    w = cfg["wecom"] if cfg.has_section("wecom") else {}
    page_url = (w.get("summary_page_url") or "").strip()
    if not page_url:
        base = (w.get("summary_base_url") or "").strip().rstrip("/")
        if base:
            page_url = f"{base}/{keyword}/{summary_name}"

    push_summary_to_wecom(keyword, len(items), page_url, cfg, do_push)


if __name__ == "__main__":
    main()
