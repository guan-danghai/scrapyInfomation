#!/usr/bin/env python3
import configparser
import json
import secrets
from pathlib import Path
import sys

import pymysql


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import wecom_notify
    cfg = configparser.ConfigParser()
    cfg.read(root / "config.ini", encoding="utf-8")

    pending = json.loads((root / "digest_pending_send.json").read_text(encoding="utf-8"))
    digest_token = (pending.get("token") or "").strip().lower()
    title = (pending.get("title") or "").strip()
    desc = (pending.get("description") or "").strip()
    if not digest_token or not title:
        raise RuntimeError("digest_pending_send.json 缺少 token/title")

    w = cfg["wecom"]
    corp_id = (w.get("corp_id") or "").strip()
    secret = (w.get("secret") or "").strip()
    agent_id = int((w.get("agent_id") or "0").strip())
    touser = "guandanghai"

    host = "http://ztb.resoftcss.com.cn:8335"
    dispatch_id = secrets.token_hex(12)
    open_url = f"{host}/ztb-test/api/card/open?dispatch_id={dispatch_id}"

    db = cfg["database"]
    conn = pymysql.connect(
        host=db.get("host"),
        port=int(db.get("port")),
        user=db.get("user"),
        password=db.get("password"),
        database=db.get("database"),
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wecom_card_dispatch_log "
                "(dispatch_id, digest_token, receiver_userid, receiver_customer, item_count, send_status, send_error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (dispatch_id, digest_token, touser, "TEST", 0, "SENT", None),
            )
    finally:
        conn.close()

    acc = wecom_notify.get_access_token(corp_id, secret)
    ok = bool(acc) and wecom_notify.send_app_textcard(
        acc,
        agent_id,
        title + "【测试】",
        (desc + "\n\n(测试链路：ztb-test)").replace("\n", "<br>"),
        open_url,
        btntxt="查看",
        touser=touser,
    )
    print(json.dumps({"ok": ok, "touser": touser, "dispatch_id": dispatch_id, "url": open_url}, ensure_ascii=False))


if __name__ == "__main__":
    main()
