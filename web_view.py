#!/usr/bin/env python3
"""
入库信息展示：提供 Web 页面与 /api/list 接口，展示 scraping_infos 表数据。
使用 config.ini 的 [database] 配置。启动：python web_view.py 或 flask --app web_view run --host 127.0.0.1
"""

import configparser
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"

app = Flask(__name__, template_folder=str(ROOT / "templates"))


def load_db_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    if not cfg.has_section("database"):
        return None
    d = cfg["database"]
    return {
        "host": d.get("host", "127.0.0.1"),
        "port": int(d.get("port", 3306)),
        "database": d.get("database", ""),
        "user": d.get("user", "root"),
        "password": d.get("password", ""),
        "charset": d.get("charset", "utf8mb4"),
    }


def _resolve_date(v: str) -> str:
    """若为 today 则返回当天 YYYY-MM-DD，否则返回原值（具体日期）。"""
    if not v or not (v := v.strip()):
        return ""
    if v.lower() == "today":
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d")
    return v


def get_default_display_date() -> str:
    """页面默认展示的日期：来自 config [scraper] start_date/end_date，today 则当天。"""
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    if not cfg.has_section("scraper"):
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d")
    s = cfg["scraper"]
    start = _resolve_date(s.get("start_date", "") or "")
    end = _resolve_date(s.get("end_date", "") or "")
    if start and end and start == end:
        return start
    if start:
        return start
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def get_connection():
    import pymysql
    db = load_db_config()
    if not db or not db.get("database"):
        return None
    return pymysql.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
        cursorclass=pymysql.cursors.DictCursor,
    )


@app.route("/api/sub_type_options", methods=["GET"])
def api_sub_type_options():
    """返回数据库中实际存在的 sub_type 值列表，供前端筛选下拉动态加载。"""
    try:
        conn = get_connection()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "options": []}), 500
    if not conn:
        return jsonify({"ok": False, "error": "未配置数据库", "options": []}), 500
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT sub_type FROM scraping_infos WHERE sub_type IS NOT NULL AND sub_type <> '' ORDER BY sub_type"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    options = [r["sub_type"] for r in rows if r.get("sub_type")]
    return jsonify({"ok": True, "options": options})


@app.route("/", methods=["GET"])
def index():
    default_date = get_default_display_date()
    resp = app.make_response(render_template("infos.html", default_date=default_date))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/default_date", methods=["GET"])
def api_default_date():
    """返回页面默认数据日期（配置的 start_date/end_date，today 则当天）。"""
    return jsonify({"ok": True, "default_date": get_default_display_date()})


@app.route("/detail/<int:record_id>", methods=["GET"])
def detail_page(record_id):
    """展示单条记录的爬取详情页（我们保存的完整正文）。"""
    return render_template("detail.html", record_id=record_id)


@app.route("/api/detail/<int:record_id>", methods=["GET"])
def api_detail(record_id):
    """返回单条记录完整内容（含 detail 字段）。"""
    try:
        conn = get_connection()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not conn:
        return jsonify({"ok": False, "error": "未配置数据库"}), 500
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, sub_type, product_related, reserve2 AS product_related_terms,
                       project_no, project_budget, winning_amount, bidding_method,
                       project_owner, owner_contact, owner_phone,
                       winning_bidder, winning_bidder_contact, winning_bidder_phone,
                       bidding_agent, bid_deadline, published_at, detail_url, detail,
                       created_at, audit_status
                FROM scraping_infos WHERE id = %s
                """,
                (record_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat() if v else None
    return jsonify({"ok": True, "item": row})


@app.route("/api/list", methods=["GET"])
def api_list():
    """分页查询 scraping_infos，支持 keyword、sub_type、product_related、date 筛选；监管相关默认排最前。"""
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(100, max(1, request.args.get("page_size", 50, type=int)))
    keyword = (request.args.get("keyword") or "").strip()
    sub_type = (request.args.get("sub_type") or "").strip()
    product_related = (request.args.get("product_related") or "").strip()
    date_filter = (request.args.get("date") or "").strip()  # YYYY-MM-DD，按 created_at 日期筛选
    audit_filter = (request.args.get("audit_filter") or "approved").strip()

    def _audit_sql_clause(af: str):
        if af == "pending":
            return "TRIM(IFNULL(audit_status,'')) = %s", ["待审核"]
        if af == "all":
            return "1=1", []
        return "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = %s", ["审核通过"]

    try:
        conn = get_connection()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "items": [], "total": 0}), 500
    if not conn:
        return jsonify({"ok": False, "error": "未配置数据库", "items": [], "total": 0}), 500

    try:
        with conn.cursor() as cur:
            where_parts = []
            params = []
            if keyword:
                where_parts.append("(title LIKE %s OR product_related LIKE %s OR reserve2 LIKE %s OR project_no LIKE %s)")
                q = f"%{keyword}%"
                params.extend([q, q, q, q])
            if sub_type:
                where_parts.append("sub_type = %s")
                params.append(sub_type)
            if product_related:
                where_parts.append("product_related LIKE %s")
                params.append(f"%{product_related}%")
            if date_filter and len(date_filter) >= 10:
                where_parts.append("DATE(created_at) = %s")
                params.append(date_filter[:10])
            aclause, aparams = _audit_sql_clause(audit_filter)
            where_parts.append(f"({aclause})")
            params.extend(aparams)
            where_sql = " AND ".join(where_parts) if where_parts else "1=1"

            # 监管相关（含监管报送）排在最前，其余按 id 降序
            order_sql = "ORDER BY (CASE WHEN product_related LIKE %s THEN 0 ELSE 1 END), id DESC"
            order_params = ["%监管报送%"]

            cur.execute(
                f"SELECT COUNT(*) AS total FROM scraping_infos WHERE {where_sql}",
                params,
            )
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT id, title, sub_type, product_related, reserve2 AS product_related_terms,
                       project_no, project_budget, winning_amount, bidding_method,
                       project_owner, owner_contact, owner_phone,
                       winning_bidder, bidding_agent,
                       published_at, bid_deadline, detail_url, created_at, audit_status
                FROM scraping_infos
                WHERE {where_sql}
                {order_sql}
                LIMIT %s OFFSET %s
                """,
                params + order_params + [page_size, (page - 1) * page_size],
            )
            items = cur.fetchall()
            for row in items:
                for k, v in row.items():
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat() if v else None
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # 仅本机可访问；需局域网访问时改为 0.0.0.0 或设环境变量 FLASK_RUN_HOST
    app.run(host=os.environ.get("FLASK_RUN_HOST", "127.0.0.1"), port=5000, debug=False)
