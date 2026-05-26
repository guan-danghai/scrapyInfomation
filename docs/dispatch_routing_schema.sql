-- 客户归并与卡片分发追踪（MySQL）
-- 执行顺序：先建 alias + lead 扩展，再建 dispatch/read 日志表
--
-- 推荐在本机执行（读 config.ini、自动补 crm_lead 列、幂等）：
--   python tools/apply_dispatch_schema.py
-- 或手工：mysql -h... -P... -u... -p 你的库名 < docs/dispatch_routing_schema.sql
--   （注意：若 MySQL 版本较老不支持 ADD COLUMN IF NOT EXISTS，请用上面 Python 脚本）

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='客户别名归并规则';

-- 分发匹配售前时可按业务条线过滤（见 config.ini lead_exclude_business_lines），例如排除「售后维护条线」
-- ALTER TABLE crm_lead ADD COLUMN business_line VARCHAR(64) DEFAULT NULL COMMENT '业务条线';

-- 若 crm_lead 已存在，仅补充以下字段（不存在才会添加）
ALTER TABLE crm_lead
  ADD COLUMN IF NOT EXISTS presale_userids VARCHAR(500) DEFAULT NULL COMMENT '企业微信userid，多人用|分隔',
  ADD COLUMN IF NOT EXISTS presale_names VARCHAR(500) DEFAULT NULL COMMENT '售前中文名，多人用|分隔（兜底可转拼音）',
  ADD COLUMN IF NOT EXISTS enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用';

-- 摘要条目 → 售前（materialize_digest_pack / write_manifest 写入；H5 /api/read/match 直接查）
CREATE TABLE IF NOT EXISTS digest_item_presale_route (
  digest_token          CHAR(32) NOT NULL COMMENT '摘要包目录名',
  record_id             BIGINT NOT NULL COMMENT 'scraping_infos.id',
  presale_userid        VARCHAR(128) NOT NULL,
  presale_display_name  VARCHAR(500) DEFAULT NULL,
  canonical_customer    VARCHAR(500) DEFAULT NULL,
  updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (digest_token, record_id, presale_userid),
  KEY idx_dt_uid (digest_token, presale_userid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='摘要售前预计算，避免打开卡片时再跑匹配';

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='企微卡片分发与访问追踪';

CREATE TABLE IF NOT EXISTS wecom_card_item_read_log (
  id                BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dispatch_id       VARCHAR(40) NOT NULL,
  record_id         BIGINT NOT NULL,
  reader_userid     VARCHAR(128) NOT NULL,
  read_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  dwell_seconds     INT UNSIGNED DEFAULT NULL COMMENT '详情页停留秒数（关闭页面前上报）',
  UNIQUE KEY uk_dispatch_record_reader (dispatch_id, record_id, reader_userid),
  KEY idx_reader_read_at (reader_userid, read_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='摘要条目详情已阅';

-- 单条转发详情链：?fr=32位hex，阅读/停留写入 wecom_card_item_read_log，dispatch_id 存 ticket 以兼容唯一键
CREATE TABLE IF NOT EXISTS forward_detail_ticket (
  ticket        CHAR(32) NOT NULL,
  record_id     BIGINT NOT NULL,
  to_userid     VARCHAR(128) NOT NULL,
  digest_token  CHAR(32) DEFAULT NULL COMMENT '从摘要页转发时可选，用于「谁已看」按 digest 过滤',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ticket),
  KEY idx_record (record_id),
  KEY idx_digest (digest_token, record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='forward_item_wecom 写入，详情 ?fr= 校验接收人';

-- 示例：招商银行北京分行、信用卡中心统一归并到 招商银行（无 pattern 唯一键，重复执行请用 NOT EXISTS 或跑 apply_dispatch_schema.py）
INSERT INTO customer_alias_map (alias_pattern, canonical_customer, match_type, priority, enabled)
SELECT '招商银行', '招商银行', 'contains', 1000, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM customer_alias_map WHERE alias_pattern = '招商银行' LIMIT 1);
INSERT INTO customer_alias_map (alias_pattern, canonical_customer, match_type, priority, enabled)
SELECT '招商银行.*', '招商银行', 'regex', 900, 1
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM customer_alias_map WHERE alias_pattern = '招商银行.*' LIMIT 1);
