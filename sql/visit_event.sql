-- 访问统计（PV/UV）明细表 v2
-- 用法：mysql -u <user> -p <db> < sql/visit_event.sql
--       或 python scripts/init_visit_event.py（可重复执行）
--
-- 口径：
--   PV = 明细行数（每次上报 1 行，写入不去重）
--   UV = COUNT(DISTINCT visitor)
--   visitor = 前端第一方匿名 ID 的 sha256 前 32 位；
--             没带 ID 时回退到 sha256(IP + UA + 服务端盐) 前 32 位。
--     原 IP / 原 UA 都不入库。
--   day = Asia/Shanghai 统计日，created_at = 上报时间（Asia/Shanghai）

CREATE TABLE IF NOT EXISTS visit_event (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  day DATE NOT NULL COMMENT '统计日，Asia/Shanghai',
  path VARCHAR(128) NOT NULL COMMENT '页面 pathname，已去 query/hash',
  visitor CHAR(32) NOT NULL COMMENT '访客身份哈希（cid 哈希 或 ip+ua 指纹）',
  visitor_src VARCHAR(8) NOT NULL DEFAULT 'fp' COMMENT '身份来源：cid=前端匿名ID / fp=IP+UA指纹',
  referrer VARCHAR(255) NULL COMMENT '来源页',
  created_at DATETIME NOT NULL COMMENT '上报时间，Asia/Shanghai',
  KEY idx_created_at (created_at),
  KEY idx_created_visitor (created_at, visitor),
  KEY idx_day_path (day, path),
  KEY idx_day_visitor (day, visitor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='站内访问明细（PV/UV）';
