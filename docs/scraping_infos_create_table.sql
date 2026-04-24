-- scraping_infos 表结构（与 Prisma 定义一致，MySQL）
-- 执行前请创建好数据库（库名与 config.ini 里 [database] database= 一致），例如：
-- CREATE DATABASE IF NOT EXISTS aippt_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- USE aippt_db;

CREATE TABLE IF NOT EXISTS scraping_infos (
  id                    INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  keyword               VARCHAR(200)   DEFAULT NULL COMMENT '关键字',
  type                  VARCHAR(100)   DEFAULT NULL COMMENT '类型',
  sub_type              VARCHAR(100)   DEFAULT NULL COMMENT '细分类型',
  title                 VARCHAR(500)   DEFAULT NULL COMMENT '标题',
  province              VARCHAR(100)   DEFAULT NULL COMMENT '省份/直辖市',
  city                  VARCHAR(100)   DEFAULT NULL COMMENT '城市',
  district              VARCHAR(100)   DEFAULT NULL COMMENT '区/县',
  detail                TEXT           DEFAULT NULL COMMENT '信息详情',
  published_at          DATETIME       DEFAULT NULL COMMENT '发布时间',
  bid_doc_fetched_at    DATETIME       DEFAULT NULL COMMENT '标书获取时间',
  project_no            VARCHAR(100)   DEFAULT NULL COMMENT '项目编号',
  project_budget        VARCHAR(100)   DEFAULT NULL COMMENT '项目预算',
  winning_amount        VARCHAR(100)   DEFAULT NULL COMMENT '中标金额',
  bidding_method        VARCHAR(100)   DEFAULT NULL COMMENT '招标方式',
  project_owner         VARCHAR(255)   DEFAULT NULL COMMENT '项目业主',
  owner_contact         VARCHAR(100)   DEFAULT NULL COMMENT '业主联系人',
  owner_phone           VARCHAR(50)    DEFAULT NULL COMMENT '业主联系电话',
  winning_bidder        VARCHAR(255)   DEFAULT NULL COMMENT '中标单位',
  winning_bidder_contact VARCHAR(100)  DEFAULT NULL COMMENT '中标单位联系人',
  winning_bidder_phone  VARCHAR(50)    DEFAULT NULL COMMENT '中标单位联系电话',
  bidding_agent         VARCHAR(255)   DEFAULT NULL COMMENT '招标代理',
  bid_deadline          DATETIME       DEFAULT NULL COMMENT '投标截止时间',
  detail_url            VARCHAR(1000)  DEFAULT NULL COMMENT '详情页地址',
  product_related       VARCHAR(500)   DEFAULT NULL COMMENT '产品相关',
  reserve1              VARCHAR(500)   DEFAULT NULL COMMENT '备用字段1',
  reserve2              VARCHAR(500)   DEFAULT NULL COMMENT '备用字段2',
  audit_status          VARCHAR(32)    NOT NULL DEFAULT '审核通过' COMMENT '待审核|审核通过',
  created_at            DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库创建时间',
  updated_at            DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_detail_url (detail_url(500))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='网站爬取信息表（招标/中标等爬取数据）';
