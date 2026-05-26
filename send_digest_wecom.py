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
import secrets
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PENDING = ROOT / "digest_pending_send.json"
CONFIG = ROOT / "config.ini"


def _out(obj: dict, code: int) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    sys.exit(code)


def _trace(msg: str) -> None:
    """写入 stderr，不污染 stdout 最后一行 JSON（Node 只解析 stdout）。"""
    print(f"[digest_trace] send_digest_wecom {msg}", file=sys.stderr, flush=True)


def _load_db_cfg():
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("database"):
        raise RuntimeError("config.ini 缺少 [database]")
    d = cfg["database"]
    return {
        "host": d.get("host", "127.0.0.1"),
        "port": int(d.get("port", "3306")),
        "user": d.get("user", "root"),
        "password": d.get("password", ""),
        "database": d.get("database", ""),
        "charset": d.get("charset", "utf8mb4"),
    }


def _connect_mysql():
    import pymysql

    db = _load_db_cfg()
    return pymysql.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _ensure_dispatch_table(conn):
    sql = """
    CREATE TABLE IF NOT EXISTS wecom_card_dispatch_log (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      dispatch_id VARCHAR(40) NOT NULL,
      digest_token VARCHAR(32) DEFAULT NULL,
      receiver_userid VARCHAR(128) NOT NULL,
      receiver_customer VARCHAR(255) DEFAULT NULL,
      item_count INT NOT NULL DEFAULT 0,
      send_status VARCHAR(16) NOT NULL DEFAULT 'SENT',
      send_error VARCHAR(500) DEFAULT NULL,
      first_read_at DATETIME DEFAULT NULL,
      last_read_at DATETIME DEFAULT NULL,
      read_count INT NOT NULL DEFAULT 0,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uk_dispatch_id (dispatch_id),
      KEY idx_receiver_created (receiver_userid, created_at),
      KEY idx_digest_token (digest_token)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    with conn.cursor() as cur:
        cur.execute(sql)


def _save_dispatch_log(conn, *, dispatch_id: str, digest_token: str, receiver_userid: str, receiver_customer: str, item_count: int, send_status: str, send_error: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wecom_card_dispatch_log
            (dispatch_id, digest_token, receiver_userid, receiver_customer, item_count, send_status, send_error)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (dispatch_id, digest_token or None, receiver_userid, receiver_customer or None, int(item_count or 0), send_status, (send_error or "")[:500] or None),
        )


def _with_dispatch(
    card_url: str, dispatch_id: str, digest_token: str = ""
) -> str:
    """
    将摘要详情链接改为「打开卡片」入口，便于记首次阅读并绑定 dispatch_id。
    同时附带 digest_token：当 Node 使用的库与发送脚本不一致导致查不到分发记录时，
    服务端可降级跳转到摘要页（见 server.js handleCardOpen）。
    """
    base = (card_url or "").strip()
    if not base or not dispatch_id:
        return base
    m = re.match(r"^(https?://[^/]+)(/.*)?$", base, re.I)
    if not m:
        return base
    host = m.group(1).rstrip("/")
    path = (m.group(2) or "").strip()
    if path.startswith("/ztb-test/") or path == "/ztb-test":
        open_path = "/ztb-test/api/card/open"
    elif path.startswith("/ztb/") or path == "/ztb":
        open_path = "/ztb/api/card/open"
    else:
        open_path = "/api/card/open"
    q = f"dispatch_id={dispatch_id}"
    dt = (digest_token or "").strip().lower()
    if dt and re.match(r"^[a-f0-9]{32}$", dt):
        q += f"&digest_token={dt}"
    return f"{host}{open_path}?{q}"


def _env_test_mode(cfg_path: Path) -> bool:
    import configparser

    c = configparser.ConfigParser()
    c.read(cfg_path, encoding="utf-8")
    if not c.has_section("environment"):
        return False
    v = (c["environment"].get("test_mode") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _rewrite_card_url_for_test(card_url: str, token_hex: str, cfg_path: Path) -> str:
    """
    test_mode 开启时，强制使用 [wecom] disclose_page_url 拼 /digest/<token>，
    避免 digest_pending_send.json 仍指向生产 /ztb/ 时误走 5000。
    """
    if not token_hex or not _env_test_mode(cfg_path):
        return card_url
    import configparser

    c = configparser.ConfigParser()
    c.read(cfg_path, encoding="utf-8")
    base = ""
    if c.has_section("wecom"):
        base = (c["wecom"].get("disclose_page_url") or "").strip()
    if not base or "work.weixin.qq.com" in base:
        return card_url
    sys.path.insert(0, str(ROOT))
    from digest_message import build_digest_pack_card_url

    return build_digest_pack_card_url(base.rstrip("/"), token_hex.lower())


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        _out({"ok": False, "error": f"stdin JSON 无效: {e}"}, 1)

    users = data.get("touser_list") or data.get("touser_ids")
    if users is not None:
        if not isinstance(users, list) or not users:
            _out({"ok": False, "error": "touser_list 须为非空数组"}, 1)
        users = [str(u).strip() for u in users if str(u).strip()]
        if not users:
            _out({"ok": False, "error": "无有效 userid"}, 1)
    else:
        users = []

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

    items = []
    if send_tok and pending and (pending.get("token") or "").strip().lower() == send_tok:
        title = (pending.get("title") or "").strip()
        desc = pending.get("description") or ""
        card_url = (pending.get("card_url") or "").strip()
        man_fp = ROOT / "digest_packs" / send_tok / "manifest.json"
        if man_fp.is_file():
            try:
                items = (json.loads(man_fp.read_text(encoding="utf-8")) or {}).get("items") or []
            except Exception:
                items = []
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

    tok_for_link = send_tok or ((pending or {}).get("token") or "").strip().lower()
    if tok_for_link and re.match(r"^[a-f0-9]{32}$", tok_for_link):
        card_url = _rewrite_card_url_for_test(card_url, tok_for_link, CONFIG)

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

    auto_route = bool(data.get("auto_route")) or (not users)
    routed_meta = {}
    digest_stats: dict = {}
    conn = None
    if auto_route:
        if not items:
            _out({"ok": False, "error": "自动路由需要摘要包 manifest 数据，请指定 digest_token"}, 1)
        try:
            conn = _connect_mysql()
            _ensure_dispatch_table(conn)
            from dispatch_router import build_user_routing_from_items, load_dispatch_config

            dcfg = load_dispatch_config(CONFIG)
            # 大批量 manifest 时 AI 慢：stdin routing_skip_ai=true，或 config.ini [dispatch] routing_skip_ai=true（stdin 未传该键时）
            skip_ai = False
            if "routing_skip_ai" in data:
                skip_ai = bool(data.get("routing_skip_ai"))
            elif cfg.has_section("dispatch"):
                v = (cfg["dispatch"].get("routing_skip_ai") or "").strip().lower()
                skip_ai = v in ("1", "true", "yes", "on", "y")
            if skip_ai:
                dcfg.enable_ai_customer_match = False
            routed_meta, digest_stats = build_user_routing_from_items(conn, items, dcfg, config_path=CONFIG)
            if not users:
                users = sorted(routed_meta.keys())
                if not users:
                    _out({"ok": False, "error": "自动路由未匹配到任何售前，请检查 customer_alias_map / crm_lead"}, 1)
            # 已指定 touser_list 时：只给名单发送，仍用本次路由元数据（匹配客户/条数），便于单人补发
        except Exception as e:
            _out({"ok": False, "error": f"自动路由失败: {e}"}, 1)
    else:
        conn = _connect_mysql()
        _ensure_dispatch_table(conn)

    tok_hint = (send_tok or ((pending or {}).get("token") or "")).strip().lower()
    tok_short = f"{tok_hint[:8]}…" if len(tok_hint) >= 8 else (tok_hint or "(pending)")
    _trace(
        f"start recipient_count={len(users)} auto_route={auto_route} digest_token={tok_short}"
    )

    success, failed = [], []
    for uid in users:
        meta = routed_meta.get(uid) or {}
        owners = meta.get("owners") or []
        item_count = int(meta.get("item_count") or 0)
        dispatch_id = secrets.token_hex(12)
        link_tok = (
            send_tok
            or ((pending or {}).get("token") or "").strip().lower()
        )
        url = _with_dispatch(card_url, dispatch_id, link_tok)
        # 匹配客户 / @售前 仅在摘要 H5 每条 card-header 展示（见 digest_pack + digest_item_presale_route），企微卡片保持标题+概览即可
        extra = ""
        ok = wecom_notify.send_app_textcard(
            token,
            agent_id,
            title,
            str(desc).replace("\n", "<br>") + extra,
            url,
            btntxt="查看",
            touser=uid,
        )
        if ok:
            success.append(uid)
            _save_dispatch_log(
                conn,
                dispatch_id=dispatch_id,
                digest_token=send_tok or ((pending or {}).get("token") or ""),
                receiver_userid=uid,
                receiver_customer="、".join(owners[:10]) if owners else "",
                item_count=item_count,
                send_status="SENT",
                send_error="",
            )
            _trace(
                f"sent_ok dispatch_id={dispatch_id} receiver={uid} item_count={item_count}"
            )
        else:
            failed.append(uid)
            _save_dispatch_log(
                conn,
                dispatch_id=dispatch_id,
                digest_token=send_tok or ((pending or {}).get("token") or ""),
                receiver_userid=uid,
                receiver_customer="、".join(owners[:10]) if owners else "",
                item_count=item_count,
                send_status="FAILED",
                send_error="send_app_textcard 失败",
            )
            _trace(
                f"sent_fail dispatch_id={dispatch_id} receiver={uid} item_count={item_count}"
            )

    if conn:
        try:
            conn.close()
        except Exception:
            pass
    if failed and not success:
        _trace(f"finish all_failed failed={failed}")
        _out({"ok": False, "error": "全部发送失败", "failed": failed}, 1)
    _trace(
        f"finish sent_count={len(success)} failed_count={len(failed)} sent={success} failed={failed}"
    )
    _out(
        {
            "ok": True,
            "sent": success,
            "failed": failed,
            "auto_route": auto_route,
            "routing_skip_ai": bool((data or {}).get("routing_skip_ai")) and auto_route,
        },
        0,
    )


if __name__ == "__main__":
    main()
