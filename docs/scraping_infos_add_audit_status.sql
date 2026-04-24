-- 为已有库增加审核状态列（执行一次即可）
-- 待审核：详情为外链占位，披露列表与企微摘要不展示，次日重爬或人工补全后可变审核通过

ALTER TABLE scraping_infos
  ADD COLUMN audit_status VARCHAR(32) NOT NULL DEFAULT '审核通过'
  COMMENT '待审核|审核通过'
  AFTER reserve2;

UPDATE scraping_infos SET audit_status = '审核通过' WHERE audit_status IS NULL OR TRIM(audit_status) = '';
