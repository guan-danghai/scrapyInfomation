#!/usr/bin/env python3
"""
测试：按多个「入库日」各生成摘要包 + 各发一条企微 textcard（默认前天、昨天、今天 → guandanghai）。

用法（项目根）：
  python push_digest_test_days.py
  python push_digest_test_days.py --touser guandanghai
  python push_digest_test_days.py --dates 2026-04-08 2026-04-09 2026-04-10
"""
from __future__ import annotations

import argparse
import configparser
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="连续推送多天的摘要卡片（digest 包测试）")
    parser.add_argument("--touser", default="guandanghai", help="企业微信 userid")
    parser.add_argument(
        "--dates",
        nargs="*",
        default=None,
        help="YYYY-MM-DD 列表；省略则默认：前天、昨天、今天",
    )
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("wecom"):
        print("config.ini 缺少 [wecom]")
        return 1
    w = cfg["wecom"]
    if not w.get("corp_id") or not w.get("agent_id") or not w.get("secret"):
        print("wecom 需配置 corp_id、agent_id、secret")
        return 1
    base = (w.get("disclose_page_url") or "").strip() or "https://work.weixin.qq.com"

    if args.dates:
        dates = [d.strip() for d in args.dates if d.strip()]
    else:
        t0 = datetime.now().date()
        dates = [
            (t0 - timedelta(days=2)).isoformat(),
            (t0 - timedelta(days=1)).isoformat(),
            t0.isoformat(),
        ]

    if not dates:
        print("未指定日期")
        return 1

    sys.path.insert(0, str(ROOT))
    from digest_message import (
        build_digest_pack_card_url,
        get_digest_payload_for_ingest_date,
        materialize_digest_pack,
    )
    import wecom_notify

    token = wecom_notify.get_access_token(
        w["corp_id"].strip(),
        w["secret"].strip(),
    )
    if not token:
        print("获取企业微信 access_token 失败")
        return 1

    print(f"将按 {len(dates)} 个日期推送给 {args.touser}：{', '.join(dates)}")

    for i, d in enumerate(dates, 1):
        print(f"\n--- [{i}/{len(dates)}] 入库日 {d} ---")
        try:
            title, description, dd = get_digest_payload_for_ingest_date(CONFIG, d)
        except Exception as e:
            print(f"  摘要文案失败: {e}")
            continue
        try:
            tok = materialize_digest_pack(CONFIG, dd)
            url = build_digest_pack_card_url(base.rstrip("/"), tok)
        except Exception as e:
            print(f"  摘要包失败: {e}")
            url = base
        ok = wecom_notify.send_app_textcard(
            token,
            int(w["agent_id"]),
            title,
            description.replace("\n", "<br>"),
            url,
            btntxt="查看",
            touser=args.touser,
        )
        print(f"  推送: {'OK' if ok else 'FAIL'}")
        print(f"  标题: {title}")
        print(f"  链接: {url}")
        if i < len(dates):
            time.sleep(1.2)

    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
