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

    # 行情：关注的合约列表（逗号分隔），首个为默认标的
    market_inst_ids: str = "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"
    market_bar: str = "1m"

    # 策略参数（双均线 + ATR 止损），标的与周期跟随行情服务
    strategy_fast: int = 9
    strategy_slow: int = 21
    strategy_atr_period: int = 14
    strategy_atr_multiple: float = 1.5
    strategy_size: str = "0.01"
    strategy_lever: int = 10
    strategy_cooldown_bars: int = 3
    strategy_trailing: bool = True

    # 量化数据管道：留空标的则复用 MARKET_INST_IDS
    quant_inst_ids: str = ""
    quant_bars: str = "1H,4H,1D"
    quant_years: float = 5.0

    @property
    def market_inst_id_list(self) -> list[str]:
        items = [item.strip().upper() for item in self.market_inst_ids.split(",")]
        return [item for item in dict.fromkeys(items) if item]

    @property
    def quant_inst_id_list(self) -> list[str]:
        source = self.quant_inst_ids or self.market_inst_ids
        items = [item.strip().upper() for item in source.split(",")]
        return [item for item in dict.fromkeys(items) if item]

    @property
    def quant_bar_list(self) -> list[str]:
        items = [item.strip() for item in self.quant_bars.split(",")]
        return [item for item in dict.fromkeys(items) if item]

    # 风控参数
    risk_max_size: str = "1"
    risk_max_notional: float = 1000.0
    risk_max_drawdown_pct: float = 20.0

    @property
    def okx_has_credentials(self) -> bool:
        return bool(self.okx_api_key and self.okx_secret_key and self.okx_passphrase)


@lru_cache
def get_settings() -> Settings:
    return Settings()
