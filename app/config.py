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
    # 逗号分隔的允许来源，默认 *
    api_cors_origins: str = "*"

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.api_cors_origins.split(",") if item.strip()]

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
            raise ConfigError(
                "数据库配置缺失，请设置环境变量：" + ", ".join(missing)
            )

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
