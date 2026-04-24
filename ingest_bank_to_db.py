#!/usr/bin/env python3
"""
将 output/银行 下爬取的 JSON 经 AI 分析后写入 scraping_infos 表。
- product_related：监管报送/IT系统建设/软件相关/其它科技相关（由 AI 或规则判定）
- sub_type：招标、中标、流标、公示、征集、磋商、谈判等
- 其它字段从正文抽取后入库
"""

import configparser
import json
import re
import sys
from pathlib import Path
from typing import Optional

# 项目根目录
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import ai_analyze

CONFIG_FILE = ROOT / "config.ini"
DEFAULT_KEYWORD = "银行"
DEFAULT_OUTPUT_SUBDIR = "银行"


def _truncate(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip()
    return s[:max_len] if len(s) > max_len else (s or None)


def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    out = {}
    # database
    if cfg.has_section("database"):
        d = cfg["database"]
        out["db"] = {
            "host": d.get("host", "127.0.0.1"),
            "port": int(d.get("port", 3306)),
            "database": d.get("database", ""),
            "user": d.get("user", "root"),
            "password": d.get("password", ""),
            "charset": d.get("charset", "utf8mb4"),
        }
    else:
        out["db"] = None
    # ai
    if cfg.has_section("ai"):
        a = cfg["ai"]
        out["ai_api_key"] = a.get("api_key", "").strip()
        out["ingest_tech_only"] = a.getboolean("ingest_tech_only", False)
        out["skip_ingest_non_financial_owner"] = a.getboolean(
            "skip_ingest_non_financial_owner", True
        )
    else:
        out["ai_api_key"] = ""
        out["ingest_tech_only"] = False
        out["skip_ingest_non_financial_owner"] = True
    return out


def get_json_files(dir_path: Path) -> list[Path]:
    """目录下所有 .json 文件，排除汇总页等。"""
    if not dir_path.is_dir():
        return []
    files = []
    for f in dir_path.iterdir():
        if f.suffix.lower() == ".json" and f.is_file():
            name = f.name
            if name.startswith("汇总") or "汇总" in name:
                continue
            files.append(f)
    return sorted(files, key=lambda p: p.name)


def row_to_db_values(row: dict) -> dict:
    """按表结构截断并转成可写入 DB 的值（VarChar 按长度限制，detail 为 TEXT 不截断）。"""
    # scraping_infos 字段长度限制（与 Prisma 一致）；detail 为 TEXT，保留全文不截断
    limits = {
        "keyword": 200, "type": 100, "sub_type": 100, "title": 500,
        "province": 100, "city": 100, "district": 100,
        "project_no": 100, "project_budget": 100, "winning_amount": 100,
        "bidding_method": 100, "project_owner": 255, "owner_contact": 100,
        "owner_phone": 50, "winning_bidder": 255, "winning_bidder_contact": 100,
        "winning_bidder_phone": 50, "bidding_agent": 255,
        "detail_url": 1000, "product_related": 500, "reserve1": 500, "reserve2": 500,
        "audit_status": 32,
        "detail": 65535,
    }
    out = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
        elif isinstance(v, str):
            # detail 等 TEXT 字段用大限制，其它未列出的字符串字段不截断（保留原样）
            out[k] = _truncate(v, limits.get(k, 65535))
        else:
            out[k] = v
    return out


def _norm_dt(v) -> Optional[str]:
    """将日期字符串转为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，供 MySQL DATETIME。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2})[：:](\d{1,2})(?:[：:](\d{1,2}))?)?", s)
    if m:
        a, b, c = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        if m.lastindex >= 5:
            h, mi = m.group(4).zfill(2), m.group(5).zfill(2)
            sec = m.group(6).zfill(2) if m.group(6) else "00"
            return f"{a}-{b}-{c} {h}:{mi}:{sec}"
        return f"{a}-{b}-{c} 00:00:00"
    return s if re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def exists_by_title(conn, title: str) -> bool:
    """判断表中是否已存在相同 title 的记录，名称重复则不再入库。"""
    if not (title or "").strip():
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scraping_infos WHERE title = %s LIMIT 1",
                (title.strip(),),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def exists_by_title_and_project_no(conn, title: str, project_no: str) -> bool:
    """重复判断：title 和 project_no 同时非空且同时匹配，才视为重复跳过。
    任意一个为空则不判重（无法确认是同一项目）。
    """
    title = (title or "").strip()
    project_no = (project_no or "").strip()
    if not title or not project_no:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scraping_infos "
                "WHERE title = %s AND project_no = %s LIMIT 1",
                (title, project_no),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def fetch_audit_status_by_detail_url(conn, detail_url: str) -> Optional[str]:
    """已存在记录时返回 audit_status（可能为空串）；不存在返回 None。"""
    u = (detail_url or "").strip()
    if not u:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit_status FROM scraping_infos WHERE detail_url = %s LIMIT 1",
            (u,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return r[0] if r[0] is not None else ""


def exists_by_detail_url(conn, detail_url: str) -> bool:
    """同一详情页 URL 只入库一次（多关键词爬取会在各关键词目录各存一份 JSON，此前会重复插入）。"""
    u = (detail_url or "").strip()
    if not u:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scraping_infos WHERE detail_url = %s LIMIT 1",
                (u,),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def update_row_by_detail_url(conn, row: dict) -> int:
    """
    按 detail_url 更新已存在记录（用于重爬后刷新正文与 AI 衍生字段）。
    返回受影响的行数（0 表示无匹配 URL）。
    """
    row = row_to_db_values(row)
    detail_url = row.get("detail_url")
    if not (detail_url or "").strip():
        return 0
    row.pop("created_at", None)
    from datetime import datetime

    row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for k in ("published_at", "bid_doc_fetched_at", "bid_deadline"):
        if row.get(k):
            row[k] = _norm_dt(row[k])
    where_url = row.pop("detail_url")
    assignments = []
    vals = []
    for k, v in row.items():
        assignments.append(f"`{k}`=%s")
        vals.append(v)
    vals.append(where_url.strip())
    sql = f"UPDATE scraping_infos SET {', '.join(assignments)} WHERE detail_url=%s"
    with conn.cursor() as cur:
        n = cur.execute(sql, vals)
    conn.commit()
    return int(n or 0)


def update_row_by_id(conn, row_id: int, row: dict) -> int:
    """按主键更新（人工补全等）。不修改 created_at。"""
    row = row_to_db_values(row)
    row.pop("created_at", None)
    row.pop("id", None)
    from datetime import datetime

    row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for k in ("published_at", "bid_doc_fetched_at", "bid_deadline"):
        if row.get(k):
            row[k] = _norm_dt(row[k])
    assignments = []
    vals = []
    for k, v in row.items():
        assignments.append(f"`{k}`=%s")
        vals.append(v)
    vals.append(row_id)
    sql = f"UPDATE scraping_infos SET {', '.join(assignments)} WHERE id=%s"
    with conn.cursor() as cur:
        n = cur.execute(sql, vals)
    conn.commit()
    return int(n or 0)


def insert_row(conn, row: dict) -> bool:
    """执行 INSERT，以 detail_url 唯一则存在时跳过（可改为 UPDATE）。"""
    row = row_to_db_values(row)
    # 日期字段转成 MySQL 可接受的格式
    for k in ("published_at", "bid_doc_fetched_at", "bid_deadline"):
        if row.get(k):
            row[k] = _norm_dt(row[k])
    # 表若未设置 DEFAULT，则必须显式写入 created_at / updated_at
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row["created_at"] = now
    row["updated_at"] = now
    cols = [k for k in row if row[k] is not None]
    placeholders = ", ".join(["%s"] * len(cols))
    columns = ", ".join(cols)
    sql = f"INSERT INTO scraping_infos ({columns}) VALUES ({placeholders})"
    vals = [row[k] for k in cols]
    try:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()
        return True
    except Exception as e:
        if "Duplicate" in str(e) or "1062" in str(e):
            return False
        raise


def _mysql_connect(db_cfg: dict):
    """
    连接 MySQL；若 errno=1049（库不存在）则尝试 CREATE DATABASE 后重连。
    库名仅允许 [a-zA-Z0-9_]，避免拼接 SQL 风险。
    """
    import pymysql
    from pymysql.err import OperationalError

    db = (db_cfg.get("database") or "").strip()
    if not db:
        raise RuntimeError("请在 config.ini 的 [database] 中配置 database 名称。")
    if not re.fullmatch(r"[A-Za-z0-9_]+", db):
        raise RuntimeError(
            "[database] 的 database 名称仅允许字母、数字、下划线，请修改 config.ini。"
        )

    common = dict(
        host=db_cfg["host"],
        port=db_cfg["port"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        charset=db_cfg["charset"],
    )
    try:
        return pymysql.connect(database=db, **common)
    except OperationalError as e:
        errno = e.args[0] if e.args else None
        if errno != 1049:
            raise
        try:
            c0 = pymysql.connect(**common)
            with c0.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            c0.commit()
            c0.close()
            print(
                f"  已自动创建数据库 `{db}`（首次部署）。"
                "若尚无 scraping_infos 表，请在库中执行 docs/scraping_infos_create_table.sql"
            )
        except Exception as e2:
            raise RuntimeError(
                f"数据库 `{db}` 在服务器上不存在，且当前账号无法自动建库（{e2}）。"
                "请 DBA 执行: CREATE DATABASE IF NOT EXISTS "
                f"`{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                "然后执行项目内 docs/scraping_infos_create_table.sql 建表。"
            ) from e2
        return pymysql.connect(database=db, **common)


def run_ingest(base_dir: Path, config: Optional[dict] = None) -> dict:
    """
    对 base_dir 下的所有关键词子目录做分析入库。
    - base_dir 一般为 output/YYYY-MM-DD，其下每子目录名为关键词，目录内为 JSON 文件。
    - 返回统计：inserted, skipped_tech, skipped_non_financial_owner, skipped_dup, errors, keywords_processed。
    """
    from datetime import datetime

    if config is None:
        config = load_config()
    db_cfg = config.get("db")
    if not db_cfg or not db_cfg.get("database"):
        raise RuntimeError("请在 config.ini 的 [database] 中配置 database 名称及其它连接信息。")

    # 确定要处理的（关键词）子目录列表
    subdirs = []
    if base_dir.is_dir():
        for d in sorted(base_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                if get_json_files(d):
                    subdirs.append((d.name, d))
    if not subdirs:
        # 兼容：base_dir 本身即单关键词目录（如 output/银行）
        if get_json_files(base_dir):
            subdirs = [(base_dir.name, base_dir)]
    if not subdirs:
        return {
            "inserted": 0,
            "updated_pending": 0,
            "skipped_tech": 0,
            "skipped_non_financial_owner": 0,
            "skipped_dup": 0,
            "errors": 0,
            "keywords_processed": [],
        }

    ingest_tech_only = config.get("ingest_tech_only", False)
    skip_non_financial_owner = config.get("skip_ingest_non_financial_owner", True)
    use_ai = bool(config.get("ai_api_key"))

    try:
        conn = _mysql_connect(db_cfg)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"数据库连接失败: {e}") from e

    # 不需要入库的信息类型（sub_type / type 命中任一则跳过）
    SKIP_INFO_TYPES = {"拍卖转让", "拍卖", "转让"}

    inserted = 0
    updated_pending = 0
    skipped_tech = 0
    skipped_non_financial_owner = 0
    skipped_type = 0
    skipped_dup = 0
    errors = 0
    keywords_processed = []

    for keyword, kw_dir in subdirs:
        json_files = get_json_files(kw_dir)
        keywords_processed.append(keyword)
        print(f"\n  关键词目录: {keyword}（{len(json_files)} 个 JSON）")
        for i, jpath in enumerate(json_files, 1):
            try:
                with open(jpath, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print(f"  [{i}] 读取失败 {jpath.name}: {e}")
                errors += 1
                continue

            title = raw.get("title") or ""
            content = raw.get("content") or ""

            ai_result = None
            if use_ai:
                ai_result = ai_analyze.analyze_with_ai(title, content, CONFIG_FILE)

            # info_type 直接取 JSON 文件头保存的列表页类型标签（如"招标公告"），不再写死
            info_type_from_file = (raw.get("info_type") or "采招信息").strip()
            row = ai_analyze.build_scraping_info_row(
                raw,
                keyword=keyword,
                info_type=info_type_from_file,
                ai_result=ai_result,
            )

            # 跳过拍卖转让等不需要入库的类型
            row_type = (row.get("type") or row.get("sub_type") or "").strip()
            if row_type in SKIP_INFO_TYPES:
                skipped_type += 1
                print(f"  [{i}] 跳过(类型={row_type}): {title[:50]}")
                continue

            if ingest_tech_only and not (row.get("product_related") or "").strip():
                skipped_tech += 1
                continue

            if skip_non_financial_owner:
                skip_nf, nf_reason = ai_analyze.should_skip_ingest_non_financial_owner(
                    ai_result,
                    title,
                    content,
                    (row.get("project_owner") or "").strip(),
                )
                if skip_nf:
                    skipped_non_financial_owner += 1
                    print(
                        f"  [{i}] 跳过(非金融采购主体): {nf_reason} | {title[:50]}"
                    )
                    continue

            detail_u = (row.get("detail_url") or "").strip()
            if exists_by_detail_url(conn, detail_u):
                st_raw = fetch_audit_status_by_detail_url(conn, detail_u)
                norm = ((st_raw if st_raw is not None else "") or "").strip() or "审核通过"
                if norm == "待审核":
                    try:
                        n = update_row_by_detail_url(conn, row)
                        if n:
                            updated_pending += 1
                            print(
                                f"  [{i}] 待审核→重爬更新: {title[:50]}... | audit={row.get('audit_status')}"
                            )
                    except Exception as e:
                        print(f"  [{i}] 待审核更新失败: {e}")
                        errors += 1
                else:
                    skipped_dup += 1
                continue
            if exists_by_title_and_project_no(conn, row.get("title") or "", row.get("project_no") or ""):
                skipped_dup += 1
                continue

            try:
                ok = insert_row(conn, row)
                if ok:
                    inserted += 1
                    print(f"  [{i}] 入库: {title[:50]}... | sub_type={row.get('sub_type')} | product_related={row.get('product_related') or '-'}")
                else:
                    skipped_dup += 1
            except Exception as e:
                print(f"  [{i}] 写入失败 {jpath.name}: {e}")
                errors += 1

    conn.close()
    print(
        f"\n完成: 入库 {inserted} 条, 待审核重爬更新 {updated_pending} 条, 跳过(类型过滤) {skipped_type} 条, 跳过(非科技) {skipped_tech} 条, "
        f"跳过(非金融采购主体) {skipped_non_financial_owner} 条, 跳过(重复) {skipped_dup} 条, 错误 {errors} 条。"
    )
    return {
        "inserted": inserted,
        "updated_pending": updated_pending,
        "skipped_type": skipped_type,
        "skipped_tech": skipped_tech,
        "skipped_non_financial_owner": skipped_non_financial_owner,
        "skipped_dup": skipped_dup,
        "errors": errors,
        "keywords_processed": keywords_processed,
    }


def main():
    import os
    if sys.platform == "win32" and (not sys.stdout.encoding or "utf" not in sys.stdout.encoding.lower()):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    config = load_config()
    db_cfg = config.get("db")
    if not db_cfg or not db_cfg.get("database"):
        print("请在 config.ini 的 [database] 中配置 database 名称及其它连接信息。")
        sys.exit(1)

    if len(sys.argv) > 1:
        base_dir = ROOT / sys.argv[1].strip().strip('"')
    else:
        from datetime import datetime
        base_dir = ROOT / "output" / datetime.now().strftime("%Y-%m-%d")
    if not base_dir.is_dir():
        legacy = ROOT / "output" / DEFAULT_OUTPUT_SUBDIR
        if legacy.is_dir():
            base_dir = legacy
            print(f"使用兼容目录: {base_dir}")
        else:
            print(f"目录不存在: {base_dir}")
            sys.exit(1)

    run_ingest(base_dir, config)


if __name__ == "__main__":
    main()
