#!/usr/bin/env python3
"""
将「当前信息」以公告形式（文本卡片）推送给 config.ini [wecom] 中配置的 to_user / to_chatid。

默认「当前信息」= 根据 config.ini 生成的运行摘要（关键词、日期范围、披露页、接收人等）。
也支持命令行传入自定义标题、描述、链接。

使用方式：
  python push_announcement_to_wecom.py
  python push_announcement_to_wecom.py --title "公告标题" --desc "公告正文"
  python push_announcement_to_wecom.py --title "通知" --desc "内容" --url "https://xxx.com"
"""

import argparse
import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"


def load_wecom_config(cfg: configparser.ConfigParser) -> dict:
    """从已读配置中解析 [wecom] 段。"""
    if not cfg.has_section("wecom"):
        return {}
    w = cfg["wecom"]
    corp_id = w.get("corp_id", "").strip()
    agent_id_raw = w.get("agent_id", "").strip()
    secret = w.get("secret", "").strip()
    to_user = w.get("to_user", "").strip() or None
    to_chatid = w.get("to_chatid", "").strip() or None
    disclose_page_url = w.get("disclose_page_url", "").strip() or None
    try:
        agent_id = int(agent_id_raw) if agent_id_raw else None
    except ValueError:
        agent_id = agent_id_raw
    return {
        "corp_id": corp_id or None,
        "agent_id": agent_id,
        "secret": secret or None,
        "to_user": to_user,
        "to_chatid": to_chatid,
        "disclose_page_url": disclose_page_url,
    }


def _scraper_summary_lines(cfg: configparser.ConfigParser) -> list[str]:
    """从 [scraper] 生成摘要行。"""
    lines = []
    if not cfg.has_section("scraper"):
        return lines
    s = cfg["scraper"]
    kws = [k.strip() for k in (s.get("keywords") or "").replace("，", ",").split(",") if k.strip()]
    if kws:
        kw_str = "、".join(kws[:8])
        if len(kws) > 8:
            kw_str += "…"
        lines.append(f"监控关键词：{kw_str}")
    start = (s.get("start_date") or "").strip()
    end = (s.get("end_date") or "").strip()
    if start or end:
        lines.append(f"时间范围：{start or '未设'} ~ {end or '未设'}")
    lines.append(f"每词最多页数：{s.get('max_pages', '3')}")
    return lines


def _wecom_summary_lines_and_url(cfg: configparser.ConfigParser) -> tuple[list[str], str]:
    """从 [wecom] 生成接收人摘要行和披露页 URL。"""
    lines = []
    url = ""
    if not cfg.has_section("wecom"):
        return lines, url
    w = cfg["wecom"]
    to_user = (w.get("to_user") or "").strip()
    if to_user:
        lines.append(f"接收人：{to_user.replace('|', '、')}")
    url = (w.get("disclose_page_url") or "").strip()
    if url:
        lines.append("点击下方按钮可打开招采信息披露页。")
    return lines, url or "https://work.weixin.qq.com"


def build_current_info_from_config(cfg: configparser.ConfigParser) -> tuple[str, str, str]:
    """
    根据 config.ini 生成「当前信息」公告：标题、描述、链接。
    :return: (title, description, url)
    """
    lines = _scraper_summary_lines(cfg)
    wecom_lines, url = _wecom_summary_lines_and_url(cfg)
    lines.extend(wecom_lines)
    description = "\n".join(lines) if lines else "当前无额外配置摘要，请查看披露页或联系管理员。"
    return "招采信息公告", description, url


def main():
    parser = argparse.ArgumentParser(description="将当前信息以公告形式推送到企业微信")
    parser.add_argument("--title", type=str, default=None, help="公告标题（不传则用 config 生成的默认标题）")
    parser.add_argument("--desc", type=str, default=None, help="公告正文（不传则用 config 生成的摘要）")
    parser.add_argument("--url", type=str, default=None, help="点击卡片跳转链接（不传则用 disclose_page_url 或默认）")
    parser.add_argument("--btn", type=str, default="查看", help="卡片按钮文字，默认「查看」")
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")

    wecom = load_wecom_config(cfg)
    if not wecom.get("corp_id") or not wecom.get("agent_id") or not wecom.get("secret"):
        print("请在 config.ini [wecom] 中配置 corp_id、agent_id、secret。")
        sys.exit(1)
    if not wecom.get("to_user") and not wecom.get("to_chatid"):
        print("请在 config.ini [wecom] 中配置 to_user 或 to_chatid。")
        sys.exit(1)

    default_title, default_desc, default_url = build_current_info_from_config(cfg)
    title = (args.title or default_title).strip() or "招采信息公告"
    description = (args.desc or default_desc).strip() or "（无正文）"
    url = (args.url or default_url).strip() or "https://work.weixin.qq.com"

    try:
        import wecom_notify
    except ImportError:
        print("未找到 wecom_notify 模块，请确保 wecom_notify.py 在项目根目录。")
        sys.exit(1)

    token = wecom_notify.get_access_token(wecom["corp_id"], wecom["secret"])
    if not token:
        print("获取企业微信 access_token 失败。")
        sys.exit(1)

    btntxt = (args.btn or "查看").strip()[:4]
    ok = wecom_notify.send_app_textcard(
        token,
        wecom["agent_id"],
        title,
        description.replace("\n", "<br>"),
        url,
        btntxt=btntxt,
        touser=wecom.get("to_user"),
        chatid=wecom.get("to_chatid"),
    )
    if ok:
        print("已以公告形式（文本卡片）推送到企业微信配置的接收人。")
    else:
        print("推送失败，请查看上方错误信息。")
        sys.exit(1)


if __name__ == "__main__":
    main()
