#!/usr/bin/env python3
"""
将「招采信息披露」以「每日招中标速递」格式文本卡片推送到企业微信。

标题/描述格式与 run_pipeline.py 一致（见 digest_message.py）：
  【每日招中标速递】YYYY.MM.DD 全国最新招中标信息汇总
  📌 今日概览
  新增招标公告：N 条
  新增中标公示：M 条

从 config.ini [wecom] 读取推送配置，从数据库按最新入库日（或全表 MAX 日）统计。

使用方式：
  python push_disclose_to_wecom.py
  python push_disclose_to_wecom.py "https://your-domain.com"
  python push_disclose_to_wecom.py "https://your-domain.com" guandanghai
  python push_disclose_to_wecom.py guandanghai
  第二、三类：URL 后或非 URL 的首个参数为企业微信成员 userid，仅向该人发应用卡片（需配置应用消息）。
"""

import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"


def load_wecom_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    if not cfg.has_section("wecom"):
        return {}
    w = cfg["wecom"]
    corp_id = w.get("corp_id", "").strip()
    agent_raw = w.get("agent_id", "").strip()
    secret = w.get("secret", "").strip()
    to_user = w.get("to_user", "").strip() or None
    to_chatid = w.get("to_chatid", "").strip() or None
    webhook = w.get("webhook_url", "").strip() or None
    page_url = w.get("disclose_page_url", "").strip() or None
    try:
        agent_id = int(agent_raw) if agent_raw else None
    except ValueError:
        agent_id = agent_raw
    return {
        "corp_id": corp_id or None,
        "agent_id": agent_id,
        "secret": secret or None,
        "to_user": to_user,
        "to_chatid": to_chatid,
        "webhook_url": webhook,
        "disclose_page_url": page_url,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    override_url = next(
        (a for a in args if a.startswith("http://") or a.startswith("https://")),
        None,
    )
    rest = [a for a in args if a != override_url]
    single_touser = rest[0] if rest else None

    cfg = load_wecom_config()

    page_url = override_url or cfg.get("disclose_page_url") or "https://work.weixin.qq.com"

    if not cfg.get("corp_id") or not cfg.get("agent_id") or not cfg.get("secret"):
        print("请在 config.ini [wecom] 中配置 corp_id、agent_id、secret。")
        sys.exit(1)
    if not cfg.get("to_user") and not cfg.get("to_chatid") and not cfg.get("webhook_url"):
        print("请在 config.ini [wecom] 中配置 to_user 或 to_chatid。")
        sys.exit(1)

    from digest_message import (
        build_digest_pack_card_url,
        get_digest_payload,
        materialize_digest_pack,
    )

    try:
        # 读 config.ini 的 start_date，与 run_pipeline.py 保持一致
        scraper_cfg = configparser.ConfigParser()
        scraper_cfg.read(CONFIG_FILE, encoding="utf-8")
        raw_start = (scraper_cfg.get("scraper", "start_date", fallback="") or "").strip()
        raw_end   = (scraper_cfg.get("scraper", "end_date",   fallback="") or "").strip()
        # 支持 today 关键字
        def _resolve(v: str) -> str:
            if v.lower() == "today":
                from datetime import datetime
                return datetime.now().strftime("%Y-%m-%d")
            return v
        ds = _resolve(raw_start) if raw_start else None
        de = _resolve(raw_end)   if raw_end   else None

        title, description, digest_date = get_digest_payload(CONFIG_FILE, ds, de)
    except Exception as e:
        print("[FAIL] 生成摘要失败: " + str(e))
        sys.exit(1)

    if digest_date:
        try:
            tok = materialize_digest_pack(CONFIG_FILE, digest_date)
            page_url = build_digest_pack_card_url(page_url.rstrip("/"), tok)
        except Exception as ex:
            print("[WARN] 摘要包生成失败，使用基础 URL:", ex)

    print(f"标题：{title}")
    print(f"描述：{description}")
    print(f"链接：{page_url}")

    try:
        import wecom_notify
    except ImportError:
        print("未找到 wecom_notify 模块。")
        sys.exit(1)

    app_touser = single_touser or cfg.get("to_user")
    app_chatid = cfg.get("to_chatid") if not single_touser else None

    ok = wecom_notify.notify_digest(
        title,
        description,
        page_url,
        webhook_url=None if single_touser else cfg.get("webhook_url"),
        app_corp_id=cfg.get("corp_id"),
        app_agent_id=cfg.get("agent_id"),
        app_secret=cfg.get("secret"),
        app_touser=app_touser,
        app_chatid=app_chatid,
        btntxt="查看",
    )
    if ok:
        print("[OK] 已以「每日招中标速递」格式推送到企业微信。")
    else:
        print("[FAIL] 推送失败，请查看上方错误信息。")
        sys.exit(1)


if __name__ == "__main__":
    main()
