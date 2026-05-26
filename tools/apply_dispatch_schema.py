#!/usr/bin/env python3
"""
将测试环境已用到的「分发/阅读追踪」相关表结构同步到 config.ini 指向的本库。

- 幂等：可反复执行，已有表/列会跳过。
- crm_lead 扩展列：用 information_schema 判断，兼容 MySQL 5.7（不依赖 ADD COLUMN IF NOT EXISTS）。
- 建表语句与 docs/dispatch_routing_schema.sql 保持一致（脚本内嵌副本，避免拆 SQL 分号出错）。

用法：
  python tools/apply_dispatch_schema.py
  python tools/apply_dispatch_schema.py --dry-run   # 只打印将要执行的 DDL，不连库
"""
from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.ini"


def _load_db():
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


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s LIMIT 1",
        (table, column),
    )
    return cur.fetchone() is not None


# 与 docs/dispatch_routing_schema.sql 中 CREATE 段落同步维护
DDL_CREATES = [
    """
CREATE TABLE IF NOT EXISTS customer_alias_map (
  id                 INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  alias_pattern      VARCHAR(255) NOT NULL COMMENT '别名/规则，如 招商银行 或 招商银行.*',
  canonical_customer VARCHAR(255) NOT NULL COMMENT '归并后的客户主品牌，如 招商银行',
  match_type         VARCHAR(16)  NOT NULL DEFAULT 'contains' COMMENT 'exact|contains|regex',
  priority           INT NOT NULL DEFAULT 100 COMMENT '规则优先级，越大越先匹配',
  enabled            TINYINT NOT NULL DEFAULT 1,
  created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_canonical_customer (canonical_customer),
  KEY idx_enabled_priority (enabled, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='客户别名归并规则'
""",
    """
CREATE TABLE IF NOT EXISTS sales_lead_confirm (
  id                    BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  customer_name         VARCHAR(500) NOT NULL COMMENT '客户名称（与采购人匹配）',
  sales                 VARCHAR(500) DEFAULT NULL COMMENT '售前姓名，多人用 | ，分隔',
  lead_row_id           BIGINT DEFAULT NULL COMMENT '原线索行参考',
  sales_from_row_id     BIGINT DEFAULT NULL,
  register_date_latest  DATE DEFAULT NULL,
  register_date_sales   DATE DEFAULT NULL,
  enabled               TINYINT NOT NULL DEFAULT 1,
  created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_customer (customer_name(191)),
  KEY idx_enabled_sales (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='销售线索确认 Excel 导入，售前路由可选主数据源'
""",
    """
CREATE TABLE IF NOT EXISTS digest_item_presale_route (
  digest_token          CHAR(32) NOT NULL COMMENT '摘要包目录名',
  record_id             BIGINT NOT NULL COMMENT 'scraping_infos.id',
  presale_userid        VARCHAR(128) NOT NULL,
  presale_display_name  VARCHAR(500) DEFAULT NULL,
  canonical_customer    VARCHAR(500) DEFAULT NULL,
  updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (digest_token, record_id, presale_userid),
  KEY idx_dt_uid (digest_token, presale_userid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='摘要售前预计算，避免打开卡片时再跑匹配'
""",
    """
CREATE TABLE IF NOT EXISTS wecom_card_dispatch_log (
  id                BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dispatch_id       VARCHAR(40) NOT NULL COMMENT '唯一分发ID',
  digest_token      VARCHAR(32) DEFAULT NULL COMMENT '摘要token',
  receiver_userid   VARCHAR(128) NOT NULL COMMENT '接收人userid',
  receiver_customer VARCHAR(255) DEFAULT NULL COMMENT '本次命中的客户主品牌(逗号分隔)',
  item_count        INT NOT NULL DEFAULT 0 COMMENT '命中条数',
  send_status       VARCHAR(16) NOT NULL DEFAULT 'SENT' COMMENT 'SENT|FAILED',
  send_error        VARCHAR(500) DEFAULT NULL,
  first_read_at     DATETIME DEFAULT NULL,
  last_read_at      DATETIME DEFAULT NULL,
  read_count        INT NOT NULL DEFAULT 0,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_dispatch_id (dispatch_id),
  KEY idx_receiver_created (receiver_userid, created_at),
  KEY idx_digest_token (digest_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='企微卡片分发与访问追踪'
""",
    """
CREATE TABLE IF NOT EXISTS wecom_card_item_read_log (
  id                BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dispatch_id       VARCHAR(40) NOT NULL,
  record_id         BIGINT NOT NULL,
  reader_userid     VARCHAR(128) NOT NULL,
  read_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  dwell_seconds     INT UNSIGNED DEFAULT NULL COMMENT '详情页停留秒数（关闭页面前上报）',
  UNIQUE KEY uk_dispatch_record_reader (dispatch_id, record_id, reader_userid),
  KEY idx_reader_read_at (reader_userid, read_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='摘要条目详情已阅'
""",
    """
CREATE TABLE IF NOT EXISTS forward_detail_ticket (
  ticket        CHAR(32) NOT NULL,
  record_id     BIGINT NOT NULL,
  to_userid     VARCHAR(128) NOT NULL,
  digest_token  CHAR(32) DEFAULT NULL COMMENT '从摘要页转发时可选，用于「谁已看」按 digest 过滤',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ticket),
  KEY idx_record (record_id),
  KEY idx_digest (digest_token, record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='forward_item_wecom 写入，详情 ?fr= 校验接收人'
""",
]

CRM_LEAD_ALTERS = [
    (
        "presale_userids",
        "ALTER TABLE crm_lead ADD COLUMN presale_userids VARCHAR(500) DEFAULT NULL "
        "COMMENT '企业微信userid，多人用|分隔'",
    ),
    (
        "presale_names",
        "ALTER TABLE crm_lead ADD COLUMN presale_names VARCHAR(500) DEFAULT NULL "
        "COMMENT '售前中文名，多人用|分隔（兜底可转拼音）'",
    ),
    (
        "enabled",
        "ALTER TABLE crm_lead ADD COLUMN enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用'",
    ),
    (
        "business_line",
        "ALTER TABLE crm_lead ADD COLUMN business_line VARCHAR(64) DEFAULT NULL "
        "COMMENT '业务条线（分发时可排除售后维护条线等）'",
    ),
]

# 表上无 alias_pattern 唯一约束，ON DUPLICATE 不可靠；用 NOT EXISTS 保证幂等
SAMPLE_ALIAS_INSERTS = [
    """
INSERT INTO customer_alias_map (alias_pattern, canonical_customer, match_type, priority, enabled)
SELECT '招商银行', '招商银行', 'contains', 1000, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM customer_alias_map WHERE alias_pattern = '招商银行' LIMIT 1)
""",
    """
INSERT INTO customer_alias_map (alias_pattern, canonical_customer, match_type, priority, enabled)
SELECT '招商银行.*', '招商银行', 'regex', 900, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM customer_alias_map WHERE alias_pattern = '招商银行.*' LIMIT 1)
""",
]


def apply_schema(*, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] 将执行：CREATE 五张表 + crm_lead 可选列 + 示例别名 INSERT", flush=True)
        for i, sql in enumerate(DDL_CREATES, 1):
            print(f"\n--- CREATE #{i} ---\n{sql.strip()}\n", flush=True)
        for col, alt in CRM_LEAD_ALTERS:
            print(f"[dry-run] 若不存在列 {col}: {alt}", flush=True)
        for s in SAMPLE_ALIAS_INSERTS:
            print(f"\n--- INSERT ---\n{s.strip()}\n", flush=True)
        return 0

    import pymysql

    db = _load_db()
    conn = pymysql.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
        cursorclass=pymysql.cursors.Cursor,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            for sql in DDL_CREATES:
                cur.execute(sql.strip())
                print(f"[ok] {sql.split()[2]} …", flush=True)

            if _table_exists(cur, "crm_lead"):
                for col, alter in CRM_LEAD_ALTERS:
                    if not _column_exists(cur, "crm_lead", col):
                        cur.execute(alter)
                        print(f"[ok] crm_lead 增加列 {col}", flush=True)
                    else:
                        print(f"[skip] crm_lead.{col} 已存在", flush=True)
            else:
                print(
                    "[warn] 库中无表 crm_lead，已跳过线索表扩展；售前分发需要该表及数据。",
                    flush=True,
                )

            for s in SAMPLE_ALIAS_INSERTS:
                cur.execute(s.strip())
            print("[ok] customer_alias_map 示例规则（不存在则插入）", flush=True)
    finally:
        conn.close()
    print(f"[done] 已同步到库 {db['database']} @ {db['host']}:{db['port']}", flush=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="同步分发/追踪相关表结构到本库")
    ap.add_argument("--dry-run", action="store_true", help="只打印 DDL，不连接数据库")
    args = ap.parse_args()
    try:
        sys.exit(apply_schema(dry_run=args.dry_run))
    except Exception as e:
        print(f"[error] {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
