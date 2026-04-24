#!/usr/bin/env python3
"""
从 stdin 读 JSON：
  {"touser_list":["id1","id2"]}  — 使用 digest_pending_send.json（跑批最新待发送）
  或增加 digest_token（32 位 hex）：若与 pending 中 token 一致则用 pending 文案；
  否则从 digest_packs/<token>/manifest.json 组标题与摘要后发送。

stdout 最后一行为 JSON 结果。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PENDING = ROOT / "digest_pending_send.json"
CONFIG = ROOT / "config.ini"


def _out(obj: dict, code: int) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    sys.exit(code)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        _out({"ok": False, "error": f"stdin JSON 无效: {e}"}, 1)

    users = data.get("touser_list") or data.get("touser_ids")
    if not isinstance(users, list) or not users:
        _out({"ok": False, "error": "touser_list 须为非空数组"}, 1)
    users = [str(u).strip() for u in users if str(u).strip()]
    if not users:
        _out({"ok": False, "error": "无有效 userid"}, 1)

    send_tok = (data.get("digest_token") or data.get("token") or "").strip().lower()
    if send_tok and not re.match(r"^[a-f0-9]{32}$", send_tok):
        _out({"ok": False, "error": "digest_token 须为 32 位 hex"}, 1)

    pending = None
    if PENDING.is_file():
        try:
            pending = json.loads(PENDING.read_text(encoding="utf-8"))
        except Exception as e:
            _out({"ok": False, "error": f"读取待发送清单失败: {e}"}, 1)

    title = ""
    desc = ""
    card_url = ""

    if send_tok and pending and (pending.get("token") or "").strip().lower() == send_tok:
        title = (pending.get("title") or "").strip()
        desc = pending.get("description") or ""
        card_url = (pending.get("card_url") or "").strip()
    elif send_tok:
        man_fp = ROOT / "digest_packs" / send_tok / "manifest.json"
        if not man_fp.is_file():
            _out({"ok": False, "error": f"未找到摘要包 digest_packs/{send_tok}/"}, 1)
        try:
            man = json.loads(man_fp.read_text(encoding="utf-8"))
        except Exception as e:
            _out({"ok": False, "error": f"读取 manifest 失败: {e}"}, 1)
        ds = (man.get("digest_date") or "").strip()
        items = man.get("items") or []
        sys.path.insert(0, str(ROOT))
        from digest_message import build_digest_pack_card_url, build_digest_title

        title = build_digest_title(ds, ds)
        zbb_types = {
            "招标公告",
            "招标预告",
            "招标变更",
            "采购信息",
            "审批公示",
        }
        zb_types = {
            "中标结果",
            "中标公示",
            "中标公告",
            "中标结果公示",
            "中标候选人公示",
            "中标",
        }
        zbb = zb = 0
        for it in items:
            st = str((it or {}).get("sub_type") or "").strip()
            if st in zbb_types:
                zbb += 1
            elif st in zb_types:
                zb += 1
        desc = "\n".join(
            [
                "📌 今日概览",
                f"新增招标公告：{zbb} 条",
                f"新增中标公示：{zb} 条",
            ]
        )
        disclose = "https://work.weixin.qq.com"
        import configparser

        _cfg = configparser.ConfigParser()
        _cfg.read(CONFIG, encoding="utf-8")
        if _cfg.has_section("wecom"):
            disclose = (
                (_cfg["wecom"].get("disclose_page_url") or "").strip() or disclose
            )
        card_url = build_digest_pack_card_url(disclose.rstrip("/"), send_tok)
    else:
        if not pending:
            _out({"ok": False, "error": "未找到 digest_pending_send.json，请先执行 run_pipeline 或指定 digest_token"}, 1)
        title = (pending.get("title") or "").strip()
        desc = pending.get("description") or ""
        card_url = (pending.get("card_url") or "").strip()

    if not title or not card_url:
        _out({"ok": False, "error": "缺少 title 或 card_url，无法发送"}, 1)

    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("wecom"):
        _out({"ok": False, "error": "config.ini 缺少 [wecom]"}, 1)
    w = cfg["wecom"]
    corp_id = (w.get("corp_id") or "").strip()
    secret = (w.get("secret") or "").strip()
    agent_id_raw = (w.get("agent_id") or "").strip()
    if not corp_id or not secret or not agent_id_raw:
        _out({"ok": False, "error": "请配置 corp_id、agent_id、secret（应用消息）"}, 1)
    try:
        agent_id = int(agent_id_raw)
    except ValueError:
        _out({"ok": False, "error": "agent_id 无效"}, 1)

    sys.path.insert(0, str(ROOT))
    import wecom_notify

    token = wecom_notify.get_access_token(corp_id, secret)
    if not token:
        _out({"ok": False, "error": "获取 access_token 失败"}, 1)

    touser = "|".join(users)
    ok = wecom_notify.send_app_textcard(
        token,
        agent_id,
        title,
        str(desc).replace("\n", "<br>"),
        card_url,
        btntxt="查看",
        touser=touser,
    )
    if ok:
        _out({"ok": True, "touser": touser}, 0)
    _out({"ok": False, "error": "send_app_textcard 失败", "touser": touser}, 1)


if __name__ == "__main__":
    main()
