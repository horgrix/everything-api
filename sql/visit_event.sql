-- 访问统计（PV/UV）明细表
-- 用法：mysql -u <user> -p <db> < sql/visit_event.sql
-- 口径与前端契约一致：day 为 Asia/Shanghai 统计日，visitor 为
-- sha256(客户端 IP + User-Agent + 服务端盐) 前 32 位，不落原 IP。

CREATE TABLE IF NOT EXISTS visit_event (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  day DATE NOT NULL COMMENT '统计日，Asia/Shanghai',
  path VARCHAR(128) NOT NULL COMMENT '页面 pathname，已去 query/hash',
  visitor CHAR(32) NOT NULL COMMENT 'sha256(ip+ua+salt) 前 32 位',
  referrer VARCHAR(255) NULL COMMENT '来源页',
  created_at DATETIME NOT NULL COMMENT '上报时间，Asia/Shanghai',
  KEY idx_day_path (day, path),
  KEY idx_day_visitor (day, visitor),
  KEY idx_visitor_path_day (visitor, path, day),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='站内访问明细（PV/UV）';
