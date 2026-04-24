#!/usr/bin/env python3
"""向指定企业微信成员发送一条「每日招中标速递」摘要卡片（与 run_pipeline 文案一致，可单独补发）。"""
import argparse
import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"


def main() -> None:
    parser = argparse.ArgumentParser(description="摘要 textcard 推送给单个 userid")
    parser.add_argument(
        "--touser",
        default="guandanghai",
        help="企业微信成员 userid，默认 guandanghai",
    )
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("wecom"):
        print("config.ini 缺少 [wecom]")
        sys.exit(1)
    w = cfg["wecom"]
    disclose_url = w.get("disclose_page_url", "").strip() or "https://work.weixin.qq.com"

    sys.path.insert(0, str(ROOT))
    from digest_message import (
        build_digest_pack_card_url,
        get_digest_payload,
        materialize_digest_pack,
    )
    import wecom_notify

    title, description, digest_date = get_digest_payload(CONFIG, None, None)
    card_url = disclose_url
    if digest_date:
        try:
            tok = materialize_digest_pack(CONFIG, digest_date)
            card_url = build_digest_pack_card_url(disclose_url.rstrip("/"), tok)
        except Exception as ex:
            print("[WARN] 摘要包生成失败，使用基础 URL:", ex)
    token = wecom_notify.get_access_token(w["corp_id"], w["secret"])
    if not token:
        sys.exit(1)
    ok = wecom_notify.send_app_textcard(
        token,
        int(w["agent_id"]),
        title,
        description.replace("\n", "<br>"),
        card_url,
        btntxt="查看",
        touser=args.touser,
    )
    print("已推送给", args.touser, "：成功" if ok else "失败")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
