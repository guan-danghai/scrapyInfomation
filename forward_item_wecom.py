#!/usr/bin/env python3
"""
转发单条招中标详情给企业微信成员（文本卡片）。
stdin JSON:
{
  "record_id": 123,
  "touser_list": ["zhangsan", "张三"],
  "link_prefix": "http://host:8335/ztb-test",
  "digest_token": "可选32位hex"
}
link_prefix 不要末尾斜杠；每人单独生成 fr 票据，卡片链接为
link_prefix + /detail/{record_id}?fr=<32位hex>，打开后上报已阅/停留记到该接收人。
优先：企业微信 userid（汉语拼音账号）。含中文时：先查 org_user 中文名 → 再自动转汉语拼音作为 userid 兜底（依赖 pypinyin，与 dispatch_router 一致）。
stdout 最后一行为 JSON。
"""
from __future__ import annotations

import json
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"


def _out(obj: dict, code: int) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)
    sys.exit(code)


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


def _ensure_forward_ticket_table(cur) -> None:
    cur.execute(
        """CREATE TABLE IF NOT EXISTS forward_detail_ticket (
      ticket CHAR(32) NOT NULL,
      record_id BIGINT NOT NULL,
      to_userid VARCHAR(128) NOT NULL,
      digest_token CHAR(32) DEFAULT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (ticket),
      KEY idx_record (record_id),
      KEY idx_digest (digest_token, record_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
    )


def _chinese_to_pinyin_userid(name: str) -> str:
    """
    中文姓名 → 连续全拼小写字母，与企业微信常见 userid 形态一致。
    无中文或转换失败则返回空串。
    """
    s = (name or "").strip()
    if not s or not re.search(r"[\u4e00-\u9fff]", s):
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        pinyin = "".join(lazy_pinyin(s, style=Style.NORMAL))
        compact = re.sub(r"[^a-zA-Z0-9]", "", pinyin).lower()
        return compact if compact else ""
    except Exception:
        return ""


def _load_dispatch_org_cfg():
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("dispatch"):
        return None
    d = cfg["dispatch"]
    return {
        "org_user_table": (d.get("org_user_table") or "org_user").strip(),
        "org_user_name_col": (d.get("org_user_name_col") or "user_name").strip(),
        "org_user_account_col": (d.get("org_user_account_col") or "user_account").strip(),
        "org_user_deleted_col": (d.get("org_user_deleted_col") or "is_deleted").strip(),
    }


def _resolve_userids(conn, raw_tokens: list[str]) -> tuple[list[str], list[str]]:
    """返回 (成功 userid 列表, 未解析的昵称列表)。"""
    dcfg = _load_dispatch_org_cfg()
    resolved = []
    missing = []
    seen = set()

    def try_org(name: str) -> str | None:
        if not dcfg:
            return None
        tbl = dcfg["org_user_table"]
        ncol = dcfg["org_user_name_col"]
        acol = dcfg["org_user_account_col"]
        dcol = dcfg["org_user_deleted_col"]
        sql = f"SELECT `{acol}` AS ac FROM `{tbl}` WHERE `{ncol}` = %s AND (`{dcol}` = 0 OR `{dcol}` IS NULL) LIMIT 1"
        with conn.cursor() as cur:
            cur.execute(sql, (name,))
            row = cur.fetchone()
        if row and (row.get("ac") or "").strip():
            return str(row["ac"]).strip()
        return None

    for raw in raw_tokens:
        s = (raw or "").strip()
        if not s:
            continue
        # 已是英文 userid：字母数字下划线等
        if re.match(r"^[a-zA-Z0-9_\-.|]+$", s) and not re.search(r"[\u4e00-\u9fff]", s):
            if "|" in s:
                for part in s.split("|"):
                    part = part.strip()
                    if part and part not in seen:
                        seen.add(part)
                        resolved.append(part)
            else:
                if s not in seen:
                    seen.add(s)
                    resolved.append(s)
            continue
        uid = try_org(s)
        if uid:
            if uid not in seen:
                seen.add(uid)
                resolved.append(uid)
            continue
        py_uid = _chinese_to_pinyin_userid(s)
        if py_uid:
            if py_uid not in seen:
                seen.add(py_uid)
                resolved.append(py_uid)
            continue
        missing.append(s)

    return resolved, missing


def main() -> None:
    # Windows 管道默认编码可能导致 stdout JSON 中文被 Node 误读；配合 PYTHONUTF8 与子进程 env
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        _out({"ok": False, "error": f"stdin JSON 无效: {e}"}, 1)

    record_id = int(data.get("record_id") or 0)
    if record_id < 1:
        _out({"ok": False, "error": "无效 record_id"}, 1)

    raw_users = data.get("touser_list") or data.get("touser_ids") or []
    if isinstance(raw_users, str):
        raw_users = [raw_users]
    if not isinstance(raw_users, list):
        _out({"ok": False, "error": "touser_list 须为数组"}, 1)

    tokens = []
    for u in raw_users:
        if not u:
            continue
        for part in re.split(r"[|,，;\s]+", str(u)):
            part = part.strip()
            if part:
                tokens.append(part)
    if not tokens:
        _out({"ok": False, "error": "无接收人"}, 1)

    link_prefix = (data.get("link_prefix") or "").strip().rstrip("/")
    if not link_prefix:
        _out({"ok": False, "error": "缺少 link_prefix（详情页所在站点前缀）"}, 1)

    conn = _connect_mysql()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, sub_type, project_owner FROM scraping_infos WHERE id = %s LIMIT 1",
                (record_id,),
            )
            row = cur.fetchone()
        if not row:
            _out({"ok": False, "error": f"记录 {record_id} 不存在"}, 1)

        title = (row.get("title") or "招中标详情").strip() or "招中标详情"
        st = (row.get("sub_type") or "").strip()
        owner = (row.get("project_owner") or "").strip()
        desc_parts = [f"类型：{st}" if st else "", f"采购人：{owner}" if owner else ""]
        description = "<br>".join([p for p in desc_parts if p]) or "点击卡片查看详情"

        users, missing_names = _resolve_userids(conn, tokens)
        if missing_names and not users:
            _out(
                {
                    "ok": False,
                    "error": "无法解析为企业微信 userid：请填写汉语拼音 userid，或与通讯录一致的账号；勿仅依赖未维护的中文名",
                    "unresolved": missing_names,
                },
                1,
            )

        digest_raw = (data.get("digest_token") or "").strip().lower()
        digest_hex = digest_raw if digest_raw and re.match(r"^[a-f0-9]{32}$", digest_raw) else None

        with conn.cursor() as cur:
            _ensure_forward_ticket_table(cur)

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
            _out({"ok": False, "error": "请配置 corp_id、agent_id、secret"}, 1)
        try:
            agent_id = int(agent_id_raw)
        except ValueError:
            _out({"ok": False, "error": "agent_id 无效"}, 1)

        sys.path.insert(0, str(ROOT))
        import wecom_notify

        token = wecom_notify.get_access_token(corp_id, secret)
        if not token:
            _out({"ok": False, "error": "获取 access_token 失败"}, 1)

        success = []
        failed = []
        failed_detail = []
        warn = []
        if missing_names:
            warn.append("部分姓名未解析已跳过：" + "、".join(missing_names))

        for uid in users:
            ticket = secrets.token_hex(16)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO forward_detail_ticket (ticket, record_id, to_userid, digest_token) VALUES (%s, %s, %s, %s)",
                    (ticket, record_id, uid, digest_hex),
                )
            url = f"{link_prefix}/detail/{record_id}?fr={ticket}"
            res = wecom_notify.send_app_textcard_result(
                token,
                agent_id,
                title[:60],
                description,
                url,
                btntxt="详情",
                touser=uid,
            )
            if res.get("ok"):
                success.append(uid)
            else:
                failed.append(uid)
                ec = res.get("errcode")
                em = res.get("errmsg") or ""
                failed_detail.append({"userid": uid, "errcode": ec, "errmsg": em})

        if not success:
            detail_txt = "；".join(
                f"{d['userid']}: errcode={d.get('errcode')} {d.get('errmsg') or ''}".strip()
                for d in failed_detail
            )
            err_msg = "全部发送失败"
            if detail_txt:
                err_msg += "（企微接口：" + detail_txt + "）"
            _out(
                {
                    "ok": False,
                    "error": err_msg,
                    "failed": failed,
                    "failed_detail": failed_detail,
                    "warning": warn,
                },
                1,
            )
        _out({"ok": True, "sent": success, "failed": failed, "warning": warn}, 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
