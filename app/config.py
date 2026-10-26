"""服务配置：全部来源于环境变量（支持 .env 文件）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class ConfigError(RuntimeError):
    """配置缺失或不合法。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- MySQL 连接（HK_DB_MYSQL_*）----
    hk_db_mysql_host: str | None = None
    hk_db_mysql_port: int = 3306
    hk_db_mysql_user: str | None = None
    hk_db_mysql_pwd: str | None = None
    hk_db_mysql_db: str | None = None

    # ---- 服务行为 ----
    # 显式数据库 URL，优先级高于 HK_DB_MYSQL_*（测试/特殊场景用）
    api_database_url: str | None = None

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # 是否允许非只读语句（默认关闭）
    api_allow_write: bool = False
    # 单次查询返回的最大行数
    api_max_rows: int = 5000
    # 查询超时（毫秒），仅 MySQL 生效
    api_query_timeout_ms: int = 30_000
    # 逗号分隔的允许来源，默认 *（放行所有来源）
    api_cors_origins: str = "*"
    # 是否允许跨域携带 Cookie / Authorization（开启后不再回显通配符 *）
    api_cors_allow_credentials: bool = False

    # ---- 访问统计 PV/UV（可选，缺失依赖时接口降级为空数据）----
    # 路由前缀；默认 /api/v09/stats，nginx 只放行 /api/v09/data/ 时可改成
    # /api/v09/data/stats 免改 nginx
    api_stats_prefix: str = "/api/v09/stats"
    # UV 匿名化盐：留空则视为未启用，全部接口返回空统计
    api_stats_salt: str = ""
    api_stats_table: str = "visit_event"
    # 同一 (visitor, path) 在该分钟数内重复上报只算 1 次 PV
    api_stats_dedup_minutes: int = 30
    # 本周统计的服务端缓存秒数，0 表示不缓存
    api_stats_cache_seconds: int = 45
    # 数据保留天数，<=0 表示不清理
    api_stats_retention_days: int = 180
    # 写库连接：默认复用 HK_DB_MYSQL_* 的主机 / 库，仅账号可单独指定（需要 INSERT 权限）
    api_stats_database_url: str | None = None
    api_stats_mysql_user: str | None = None
    api_stats_mysql_pwd: str | None = None

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.api_cors_origins.split(",") if item.strip()]

    @property
    def stats_enabled(self) -> bool:
        """有盐即视为启用；盐为空时接口降级为空统计，前端组件自动隐藏。"""
        return bool(self.api_stats_salt.strip())

    def stats_url(self) -> URL | str:
        """访问统计写库连接串。

        优先级：API_STATS_DATABASE_URL > API_STATS_MYSQL_USER/PWD + HK_DB_MYSQL_HOST/DB
        > 主库连接（HK_DB_MYSQL_*）。
        """
        if self.api_stats_database_url:
            return self.api_stats_database_url
        if self.api_stats_mysql_user:
            required = {
                "HK_DB_MYSQL_HOST": self.hk_db_mysql_host,
                "API_STATS_MYSQL_PWD": self.api_stats_mysql_pwd,
                "HK_DB_MYSQL_DB": self.hk_db_mysql_db,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ConfigError("访问统计写库配置缺失，请设置环境变量：" + ", ".join(missing))
            return URL.create(
                drivername="mysql+pymysql",
                username=self.api_stats_mysql_user,
                password=self.api_stats_mysql_pwd,
                host=self.hk_db_mysql_host,
                port=self.hk_db_mysql_port,
                database=self.hk_db_mysql_db,
                query={"charset": "utf8mb4"},
            )
        return self.mysql_url()

    def mysql_url(self) -> URL | str:
        """构造 SQLAlchemy 连接串。缺少必填项时抛出 ConfigError。"""
        if self.api_database_url:
            return self.api_database_url

        required = {
            "HK_DB_MYSQL_HOST": self.hk_db_mysql_host,
            "HK_DB_MYSQL_USER": self.hk_db_mysql_user,
            "HK_DB_MYSQL_PWD": self.hk_db_mysql_pwd,
            "HK_DB_MYSQL_DB": self.hk_db_mysql_db,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError("数据库配置缺失，请设置环境变量：" + ", ".join(missing))

        return URL.create(
            drivername="mysql+pymysql",
            username=self.hk_db_mysql_user,
            password=self.hk_db_mysql_pwd,
            host=self.hk_db_mysql_host,
            port=self.hk_db_mysql_port,
            database=self.hk_db_mysql_db,
            query={"charset": "utf8mb4"},
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
