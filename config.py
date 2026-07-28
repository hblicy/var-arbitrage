"""Configuration module for the Nado ↔ Variational arbitrage monitor.

配置说明：
- 所有参数均可通过环境变量覆盖，便于在测试网 / 主网上切换。
- 通过 dataclass 组织配置，方便在代码中直接引用属性，并保持默认值清晰。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import copy
import os
from dotenv import load_dotenv

# 加载 .env 文件以便后续 _env_ 函数能正确读取
load_dotenv()


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量，失败时回退到默认值。"""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，失败时回退到默认值。"""
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Endpoint:
    """单个接口端点的配置描述。"""

    # HTTP 请求方法，例如 GET / POST
    method: str = "GET"
    # 相对路径，最终组合为 base_url + path
    path: str = ""
    # URL 查询参数
    params: Dict[str, object] = field(default_factory=dict)
    # POST 请求的 JSON 负载模板
    json: Optional[Dict[str, object]] = None
    # 自定义请求头
    headers: Dict[str, str] = field(default_factory=dict)
    # 响应 JSON 中需要逐级取值的 key 列表
    response_path: List[str] = field(default_factory=list)
    # 行情返回中的交易对字段名
    symbol_key: str = "symbol"
    # 行情返回中的价格字段名
    price_key: str = "price"
    # 行情返回中的资金费率字段名
    funding_key: Optional[str] = None
    # 行情返回中的 24h 成交量字段名
    volume_key: Optional[str] = None
    # 可选的时间戳字段名
    timestamp_key: Optional[str] = None

    def clone(self) -> "Endpoint":
        return copy.deepcopy(self)


@dataclass
class ExchangeSettings:
    """单个交易所的配置项。"""

    # 交易所名称，仅用于日志和标识
    name: str
    # API 基础域名
    base_url: str
    # WebSocket 接入点（可选）
    ws_url: Optional[str] = None
    # 请求超时时间（秒）
    timeout: float = 10.0
    # 如果需要限制交易所返回的特定合约，可在此列出
    symbols: List[str] = field(default_factory=list)
    # 将交易所原始符号映射到统一符号，例如 BTC-PERP -> BTCUSDT
    symbol_overrides: Dict[str, str] = field(default_factory=dict)
    # 黑名单：需要过滤掉的交易对
    excluded_symbols: Set[str] = field(default_factory=set)
    # 正则表达式模式，用于从原始符号中提取核心资产名 (e.g. BTC)
    # 捕获组应返回资产名
    symbol_pattern: Optional[str] = None
    # 行情接口配置
    price_endpoint: Endpoint = field(default_factory=Endpoint)
    # 资金费率接口配置（可选）
    funding_endpoint: Optional[Endpoint] = None
    # 交易所吃单手续费（基点）
    taker_bps: float = 5.0
    # Variational 无公开 API 时使用的抓取模式（scrape / disable）
    scrape_mode: Optional[str] = None
    # 抓取模式下使用的 CSS 选择器集合
    scrape_selectors: Dict[str, List[str]] = field(default_factory=dict)
    # 请求额外 Header（如 User-Agent）
    extra_headers: Dict[str, str] = field(default_factory=dict)
    # 是否启用该交易所
    enabled: bool = True
    # 最小 24h 交易额过滤（单位 USD）
    min_volume_usd: Optional[float] = None
    # 资金费周期元数据的最短刷新间隔（秒）
    metadata_refresh_seconds: int = _env_int("FUNDING_METADATA_REFRESH_SECONDS", 60)


@dataclass
class ThresholdSettings:
    """阈值配置：决定何时触发套利提醒。"""

    # --- 面板显示门槛 ---
    dashboard_min_spread_bps: float = _env_float("DASHBOARD_MIN_SPREAD_BPS", 20.0)
    dashboard_min_funding_bps: float = _env_float("DASHBOARD_MIN_FUNDING_BPS", 10.0)
    dashboard_min_total_bps: float = _env_float("DASHBOARD_MIN_TOTAL_BPS", 20.0)

    # --- 微信推送门槛 ---
    notify_min_spread_bps: float = _env_float("NOTIFY_MIN_SPREAD_BPS", 100.0)
    notify_min_funding_bps: float = _env_float("NOTIFY_MIN_FUNDING_BPS", 50.0)
    notify_min_total_bps: float = _env_float("NOTIFY_MIN_TOTAL_BPS", 100.0)
    dashboard_max_cover_hours: float = _env_float("DASHBOARD_MAX_COVER_HOURS", 24.0)
    notify_max_cover_hours: float = _env_float("NOTIFY_MAX_COVER_HOURS", 24.0)

    # --- 资金费反转时间阈值 (分钟) ---
    # 只有当资金费率持续不利超过此时间才触发退出警报
    funding_reversal_threshold_minutes: int = _env_int("FUNDING_REVERSAL_THRESHOLD_MINUTES", 5)
    
    # 平仓信号触发阈值（净资金费率 bps，8h basis），低于此值且持续一段时间才触发
    # -33.3 bps (8h) ≈ -100 bps (24h) ≈ 24小时亏损1%
    exit_min_net_funding_bps: float = _env_float("EXIT_MIN_NET_FUNDING_BPS", -33.3)

    # 兼容性/旧版保留 (可选)
    min_spread_bps: float = _env_float("MIN_SPREAD_BPS", 100.0)
    min_funding_diff: float = _env_float("MIN_FUNDING_BPS", 80.0) / 10000.0
    # 过滤低流动性的交易对（单位 USD）- 默认提升到 20w
    min_volume_usd: float = _env_float("GLOBAL_MIN_VOLUME_USD", 200_000)
    
    # 最大价格偏差容忍度（百分比）：用于拦截不同交易所同名但实际上不是同一个币的假交易对（如两边价格相差 50% 以上）
    max_price_deviation_pct: float = _env_float("MAX_PRICE_DEVIATION_PCT", 15.0)


@dataclass
class FeeSettings:
    """交易成本估算参数。"""

    # Nado 单边吃单手续费（基点）
    nado_taker_bps: float = _env_float("NADO_TAKER_FEE_BPS", 5.0)
    # Variational 单边吃单手续费（基点）
    variational_taker_bps: float = _env_float("VARIATIONAL_TAKER_FEE_BPS", 5.0)
    # 假设的双边滑点（基点），每条腿各一次
    slippage_bps: float = _env_float("DEFAULT_SLIPPAGE_BPS", 10.0)


@dataclass
class ScheduleSettings:
    """任务调度相关配置。"""

    # 监控轮询间隔（秒）
    interval_seconds: int = _env_int("ARBITRAGE_SCAN_INTERVAL", 10)
    # 单所行情成功采集后的最大可用时长；超过后不得参与机会计算
    market_stale_seconds: int = _env_int("MARKET_STALE_SECONDS", 30)


@dataclass
class NotificationSettings:
    """告警推送设置。"""

    # 企业微信 Webhook 地址
    wechat_webhook: str = os.getenv("NADO_VARIATIONAL_WECHAT_WEBHOOK", "")
    # 通知冷却时间（秒），默认 1800 秒 (30 分钟)
    cooldown_seconds: int = _env_int("NOTIFICATION_COOLDOWN_SECONDS", 1800)
    # 触发通知的交易所白名单（逗号分隔），为空则通知所有
    notification_exchanges: List[str] = field(default_factory=lambda: [
        s.strip().lower() for s in os.getenv("NOTIFICATION_EXCHANGES", "").split(",") if s.strip()
    ])
    # 通知时间偏移（分钟）：只在每个整点后的第N分钟开始发送通知
    # 例如设为10，则 XX:10~XX:59 可发送，XX:00~XX:09 跳过。设为0表示不限制。
    notify_minute_offset: int = _env_int("NOTIFY_MINUTE_OFFSET", 10)



@dataclass
class EntryCheckSettings:
    """Manual entry verification limits. This monitor never submits orders."""

    notional_usd: float = _env_float("ENTRY_CHECK_NOTIONAL_USD", 1_000.0)
    safety_buffer_bps: float = _env_float("ENTRY_CHECK_SAFETY_BUFFER_BPS", 20.0)
    max_quote_age_ms: int = _env_int("ENTRY_CHECK_MAX_QUOTE_AGE_MS", 2_000)
    max_leg_skew_ms: int = _env_int("ENTRY_CHECK_MAX_LEG_SKEW_MS", 1_000)
    book_depth_limit: int = _env_int("ENTRY_CHECK_BOOK_DEPTH_LIMIT", 100)


@dataclass
class Settings:
    """全局配置入口，供其它模块引用。"""

    # 运行时持久化配置文件
    runtime_config_file: str = "exchange_configs.json"

    # 监控的交易对，如果为空则自动发现共有币种
    tracked_symbols: List[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("TRACKED_SYMBOLS", "").split(",") if s.strip()])
    
    # 黑名单系统：全局屏蔽特定的交易对
    blacklist_symbols: List[str] = field(default_factory=lambda: [s.strip().upper() for s in os.getenv("BLACKLIST_SYMBOLS", "").split(",") if s.strip()])
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    thresholds: ThresholdSettings = field(default_factory=ThresholdSettings)
    fees: FeeSettings = field(default_factory=FeeSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    entry_check: EntryCheckSettings = field(default_factory=EntryCheckSettings)
    nado: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Nado",
        base_url=os.getenv("NADO_BASE_URL", "https://archive.test.nado.xyz"),
        # 支持自定义超时（可通过环境变量覆盖）
        timeout=_env_float("NADO_HTTP_TIMEOUT", 10.0),
        symbol_overrides={
            "BTC-PERP_USDT0": "BTCUSDT",
            "ETH-PERP_USDT0": "ETHUSDT",
            "BNB-PERP_USDT0": "BNBUSDT",
            "SOL-PERP_USDT0": "SOLUSDT",
        },
        symbol_pattern=r"^([A-Z0-9]+)-PERP_USDT\d+$",
        price_endpoint=Endpoint(
            method="GET",
            path="/v2/tickers",
            params={"market": "perp"},
            response_path=[],
            symbol_key="ticker_id",
            price_key="last_price",
            funding_key="funding_rate_24h",
            volume_key="quote_volume",
        ),
        taker_bps=_env_float("NADO_TAKER_FEE_BPS", 3.5),  # Nado: 0.01% maker / 0.035% taker
    ))
    variational: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Variational",
        base_url=os.getenv("VARIATIONAL_BASE_URL", "https://omni.variational.io"),
        timeout=_env_float("VARIATIONAL_HTTP_TIMEOUT", 10.0),
        symbol_overrides={
            "BTC-PERP": "BTCUSDT",
            "ETH-PERP": "ETHUSDT",
            "BNB-PERP": "BNBUSDT",
            "SOL-PERP": "SOLUSDT",
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "BNB": "BNBUSDT",
            "SOL": "SOLUSDT",
        },
        symbol_pattern=r"^([A-Z0-9]+)(?:-PERP)?$",
        # 默认启用抓取模式，若官方发布 API 可改为其它模式
        scrape_mode=os.getenv("VARIATIONAL_SCRAPE_MODE", "scrape"),
        scrape_selectors={
            # 行级选择器，匹配 /markets 页面中的资产行
            "row": [
                "div.grid.grid-cols-markets",
                "div[role='row']",
            ],
            # 交易对字段选择器 (第一个子 div 通常包含符号)
            "symbol": [
                "div:nth-child(1) span.font-bold",
                "div:nth-child(1)",
            ],
            # 最新价字段选择器 (第三个子 div 是价格)
            "price": [
                "div:nth-child(3) span",
                "div:nth-child(3)",
            ],
            # 资金费率字段选择器 (第七个子 div 是年化资金费率)
            "funding": [
                "div:nth-child(7) span",
                "div:nth-child(7)",
            ],
            # 24 小时成交量字段选择器 (第二个子 div 是成交量)
            "volume": [
                "div:nth-child(2)",
            ],
        },
        price_endpoint=Endpoint(
            method="GET",
            path="/api/metadata/supported_assets",
            response_path=[],
            symbol_key="asset",
            price_key="price",
            funding_key="funding_rate",
            volume_key="volume_24h",
        ),
        taker_bps=_env_float("VARIATIONAL_TAKER_FEE_BPS", 0.0),  # Variational: 0% maker / 0% taker
        extra_headers={
            "User-Agent": os.getenv(
                "VARIATIONAL_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://omni.variational.io/markets",
            "Origin": "https://omni.variational.io",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": os.getenv("VARIATIONAL_COOKIE", ""),
        },
    ))
    binance: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Binance",
        base_url=os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com"),
        timeout=_env_float("BINANCE_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        symbol_pattern=r"^([A-Z0-9]+)$",
        price_endpoint=Endpoint(
            method="GET",
            path="/fapi/v1/ticker/price",
            response_path=[],
            symbol_key="symbol",
            price_key="price",
        ),
        funding_endpoint=Endpoint(
            method="GET",
            path="/fapi/v1/premiumIndex",
            response_path=[],
            symbol_key="symbol",
            funding_key="lastFundingRate",
        ),
        taker_bps=5.0,  # Binance: 0.02% maker / 0.05% taker
    ))
    lighter: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Lighter",
        base_url=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        timeout=_env_float("LIGHTER_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        symbol_pattern=r"^([A-Z0-9]+)$",
        price_endpoint=Endpoint(
            method="GET",
            path="/api/v1/orderBookDetails",
            response_path=["order_book_details"],
            symbol_key="symbol",
            price_key="last_trade_price",
        ),
        funding_endpoint=Endpoint(
            method="GET",
            path="/api/v1/funding-rates",
        ),
        taker_bps=0.0,  # Lighter: 0% maker / 0% taker
    ))
    hyperliquid: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Hyperliquid",
        base_url=os.getenv("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        timeout=_env_float("HYPERLIQUID_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        # Hyperliquid uses bare names: BTC, ETH, SOL → collector auto-appends USDT
        symbol_pattern=None,
        price_endpoint=Endpoint(
            method="POST",
            path="/info",
        ),
        taker_bps=_env_float("HYPERLIQUID_TAKER_FEE_BPS", 4.5),  # Hyperliquid: 0.015% maker / 0.045% taker
    ))
    aster: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Aster",
        base_url=os.getenv("ASTER_BASE_URL", "https://fapi.asterdex.com"),
        timeout=_env_float("ASTER_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        symbol_pattern=r"^([A-Z0-9]+)USDT$",
        price_endpoint=Endpoint(
            method="GET",
            path="/fapi/v1/ticker/24hr",
            symbol_key="symbol",
            price_key="lastPrice",
            volume_key="quoteVolume",
        ),
        funding_endpoint=Endpoint(
            method="GET",
            path="/fapi/v1/premiumIndex",
            symbol_key="symbol",
            funding_key="lastFundingRate",
        ),
        taker_bps=_env_float("ASTER_TAKER_FEE_BPS", 4.0),  # Aster: 0% maker / 0.04% taker
        extra_headers={
            "Accept": "application/json",
            "Referer": "https://www.asterdex.com/",
            "Origin": "https://www.asterdex.com",
            "User-Agent": os.getenv(
                "ASTER_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        },
        min_volume_usd=_env_float("ASTER_MIN_VOLUME_USD", 0.0),
    ))
    backpack: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Backpack",
        base_url=os.getenv("BACKPACK_BASE_URL", "https://api.backpack.exchange"),
        timeout=_env_float("BACKPACK_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        # Backpack uses BTC_USDC_PERP format in tickers
        symbol_pattern=r"^([A-Z0-9]+)_USDC_PERP$",
        price_endpoint=Endpoint(
            method="GET",
            path="/api/v1/tickers",
            symbol_key="symbol",
            price_key="lastPrice",
            volume_key="quoteVolume",
        ),
        funding_endpoint=Endpoint(
            method="GET",
            path="/api/v1/markPrices",
            symbol_key="symbol",
            funding_key="fundingRate",
        ),
        taker_bps=5.0,  # Backpack: 0.02% maker / 0.05% taker
    ))
    grvt: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="Grvt",
        base_url=os.getenv("GRVT_BASE_URL", "https://market-data.grvt.io"),
        timeout=_env_float("GRVT_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        symbol_pattern=r"^([A-Z0-9]+)_([A-Z0-9]+)_Perp$",
        price_endpoint=Endpoint(
            method="POST",
            path="/full/v1/mini",
            symbol_key="instrument",
            price_key="mark_price",
        ),
        funding_endpoint=Endpoint(
            method="POST",
            path="/full/v1/funding",
            symbol_key="instrument",
            funding_key="funding_rate",
        ),
        taker_bps=4.5,  # GRVT: -0.0001% maker / 0.045% taker
    ))
    ondoperps: ExchangeSettings = field(default_factory=lambda: ExchangeSettings(
        name="OndoPerps",
        base_url=os.getenv("ONDOPERPS_BASE_URL", "https://api.ondoperps.xyz"),
        timeout=_env_float("ONDOPERPS_HTTP_TIMEOUT", 10.0),
        symbol_overrides={},
        symbol_pattern=r"^([A-Z0-9]+)-USD\.P$",
        price_endpoint=Endpoint(
            method="GET",
            path="/v1/perps/contracts",
            response_path=["result"],
            symbol_key="market",
            price_key="lastPrice",
            funding_key="nextFundingRate",
            volume_key="quoteVolume",
        ),
        taker_bps=_env_float("ONDOPERPS_TAKER_FEE_BPS", 3.5),  # OndoPerps: 0.015% maker / 0.035% taker
        extra_headers={
            "Accept": "application/json",
            "Referer": "https://app.ondoperps.xyz/",
            "Origin": "https://app.ondoperps.xyz",
            "User-Agent": os.getenv(
                "ONDOPERPS_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        },
        min_volume_usd=_env_float("ONDOPERPS_MIN_VOLUME_USD", 0.0),
    ))

    @property
    def exchanges(self) -> Dict[str, ExchangeSettings]:
        """Return a map of all exchange configurations."""
        return {
            "binance": self.binance,
            "variational": self.variational,
            "nado": self.nado,
            "hyperliquid": self.hyperliquid,
            "aster": self.aster,
            "lighter": self.lighter,
            "backpack": self.backpack,
            "grvt": self.grvt,
            "ondoperps": self.ondoperps,
        }

    def save_runtime_config(self):
        """Save exchange enabled states to disk."""
        import json
        states = {k: v.enabled for k, v in self.exchanges.items()}
        try:
            with open(self.runtime_config_file, "w") as f:
                json.dump(states, f, indent=4)
        except Exception as e:
            print(f"Error saving runtime config: {e}")

    def load_runtime_config(self):
        """Load exchange enabled states from disk."""
        import json
        if not os.path.exists(self.runtime_config_file):
            return
        try:
            with open(self.runtime_config_file, "r") as f:
                states = json.load(f)
                all_exchanges = self.exchanges
                for k, enabled in states.items():
                    if k in all_exchanges:
                        all_exchanges[k].enabled = enabled
        except Exception as e:
            print(f"Error loading runtime config: {e}")


settings = Settings()
settings.load_runtime_config()
"""Default settings instance used by the rest of the application."""
