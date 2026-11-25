"""应用配置：统一从 backend/.env 读取，禁止在代码中硬编码密钥。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AutoTrade"
    app_env: str = "dev"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # OKX 凭据（模拟盘 Key 与主网 Key 是两套，不要混用）
    okx_api_key: str = ""
    okx_secret_key: str = ""
    okx_passphrase: str = ""

    # 交易所连接
    okx_base_url: str = "https://www.okx.com"
    okx_simulated: bool = True
    okx_timeout: float = 10.0
    # 可选：本地代理，如 http://127.0.0.1:7890
    okx_proxy: str = ""
    # 可选：DNS 被污染时，把 OKX 域名强制解析到指定 IP（TLS 证书仍按域名校验）
    okx_resolve_ip: str = ""

    # 存储
    database_url: str = "sqlite:///./autotrade.db"

    @property
    def okx_has_credentials(self) -> bool:
        return bool(self.okx_api_key and self.okx_secret_key and self.okx_passphrase)


@lru_cache
def get_settings() -> Settings:
    return Settings()
