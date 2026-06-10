import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
import sys
import secrets
import re

# Ensure the parent directory is in the path so we can import our modules
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import settings
from collectors.nado import NadoCollector
from collectors.variational import VariationalCollector
from collectors.binance import BinanceCollector
from analyzer import analyse_markets
from notifier import WeChatNotifier
from position_tracker import (
    add_position, remove_position,
    check_exit_signals, estimate_position_pnl, init_tracker,
    update_pre_settlement_rates, accumulate_funding
)

from logging.handlers import RotatingFileHandler

# Configure logging
# Capture ALL logs (Root, Uvicorn, App) to file for diagnosis
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# Use RotatingFileHandler to prevent log file from growing indefinitely
# 10MB per file, max 5 backup files
file_handler = RotatingFileHandler("logs/api.log", maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)

# Configure Root Logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)

# Configure Application Logger
logger = logging.getLogger("ArbitrageAPI")
logger.setLevel(logging.INFO)
# (Root handler will cover it, but explicit addition is fine)

# Force Uvicorn loggers to use our file handler
logging.getLogger("uvicorn").addHandler(file_handler)
logging.getLogger("uvicorn.error").addHandler(file_handler)
logging.getLogger("uvicorn.access").addHandler(file_handler)

logger.info("Global Logging initialized (Root + Uvicorn + app).")


from contextlib import asynccontextmanager, suppress

if sys.platform == 'win32':
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = FastAPI(title="Nado-Variational Arbitrage Monitor")

# ===========================
# Position Management Auth
# ===========================
security = HTTPBasic()
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _validate_username(username: str) -> None:
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="Username can only contain letters, numbers, '_' and '-'",
        )


def _validate_role(role: str) -> None:
    if role not in ("admin", "trader"):
        raise HTTPException(status_code=400, detail="role must be admin or trader")

def _verify_password(plain: str, hashed: str) -> bool:
    """Verify Basic Auth password against the users table hash."""
    try:
        import bcrypt
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ImportError:
        return secrets.compare_digest(plain, hashed)


def _hash_password(plain: str) -> str:
    """Hash a password for storing in the users table."""
    import bcrypt
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    from db import get_user_by_username
    return get_user_by_username(username)


def get_current_user(credentials: HTTPBasicCredentials = Depends(security)) -> Dict[str, Any]:
    """Verify HTTP Basic Auth credentials against SQLite users."""
    user = _get_user_by_username(credentials.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not user.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disabled",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not _verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return {k: v for k, v in user.items() if k != "password_hash"}


def require_admin(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require admin role for user management APIs."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user

# Use startup events as they are sometimes more reliable with Uvicorn on Windows
@app.on_event("startup")
async def startup_event():
    logger.info("Application Startup. Initializing collectors...")
    global collectors_hub, notifier
    try:
        from db import create_user, get_all_users, init_db
        init_db()
        if not get_all_users(include_disabled=True):
            create_user(
                username=os.getenv("POSITION_USER", "admin"),
                password_hash=_hash_password(os.getenv("POSITION_PASS", "admin")),
                display_name="Administrator",
                role="admin",
                enabled=True,
            )
            logger.info("Default admin user created: %s", os.getenv("POSITION_USER", "admin"))
        init_tracker()
        logger.info("Position database initialized.")
    except Exception as e:
        logger.error(f"Error initializing position database: {e}")

    try:
        from collectors.factory import create_collector
        for key, cfg in settings.exchanges.items():
            if not cfg.enabled:
                continue
            try:
                logger.info(f"Initializing collector: {key}")
                collectors_hub[key] = create_collector(key, cfg)
                logger.info(f"Collector {key} initialized.")
            except ValueError as e:
                logger.warning(str(e))
            except Exception as e:
                logger.error(f"Error initializing collector {key}: {e}")
    except Exception as e:
        logger.error(f"Error in startup collector initialization: {e}")
    
    # Initialize global notifier singleton
    notifier = WeChatNotifier(settings.notifications)
    logger.info(f"Notifier initialized (Cooldown: {settings.notifications.cooldown_seconds}s)")
    
    logger.info("Starting background task...")
    asyncio.create_task(update_data_loop())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application Shutdown.")

# Enable CORS for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MarketInfo(BaseModel):
    price: float
    funding_rate: Optional[float]
    volume_24h: Optional[float]
    native_interval: int

class ArbitrageOpportunity(BaseModel):
    symbol: str
    direction: str
    spread_bps: float
    total_net_bps: float
    total_apr: Optional[float] = 0.0
    base_interval: int
    details: Dict[str, Any]

class DashboardData(BaseModel):
    markets: Dict[str, Dict[str, Optional[MarketInfo]]]
    opportunities: List[ArbitrageOpportunity]
    reasons: Dict[str, str] = {}
    symbol_max_intervals: Dict[str, int] = {}
    last_update: str
    data_version: int = 0

# Shared state
latest_data = {
    "markets": {},
    "opportunities": [],
    "reasons": {},
    "symbol_max_intervals": {},
    "last_update": "Never",
    "data_version": 0,
    "last_notified_at": 0,  # Unix timestamp
    "raw_exchanges_data": {} # Store latest successful fetch for re-analysis
}

def bump_data_version():
    latest_data["data_version"] = latest_data.get("data_version", 0) + 1

# Global collectors registry
collectors_hub: Dict[str, Any] = {}
# Global notifier (singleton to persist _last_sent state)
notifier: WeChatNotifier = None

async def perform_analysis():
    """Analyze current raw data from ALL exchanges (multi-user: frontend filters locally)."""
    exchanges_data = latest_data["raw_exchanges_data"]
    
    if not exchanges_data:
        latest_data["opportunities"] = []
        latest_data["reasons"] = {}
        latest_data["markets"] = {}
        latest_data["symbol_max_intervals"] = {}
        bump_data_version()
        return []

    # Find all relevant symbols from ALL exchanges
    if not settings.tracked_symbols:
        all_symbols = set()
        for data in exchanges_data.values():
            all_symbols.update(data.keys())
        symbols_to_analyze = sorted(list(all_symbols))
    else:
        symbols_to_analyze = settings.tracked_symbols

    # 应用全局黑名单过滤
    if getattr(settings, "blacklist_symbols", None):
        blacklist = set(settings.blacklist_symbols)
        # 支持黑名单里写 CRCL，能匹配到 CRCLUSDT 或 CRCL-USDT
        symbols_to_analyze = [
            sym for sym in symbols_to_analyze 
            if not any(b in sym for b in blacklist)
        ]

    if not symbols_to_analyze:
        latest_data["opportunities"] = []
        latest_data["markets"] = {}
        latest_data["reasons"] = {}
        latest_data["symbol_max_intervals"] = {}
        bump_data_version()
        return []

    # Analyze opportunities across ALL exchanges
    opportunities, reasons, symbol_max_intervals = analyse_markets(symbols_to_analyze, exchanges_data, settings)
    
    # Prepare dashboard format - include ALL exchanges
    markets_display = {}
    for sym in symbols_to_analyze:
        sym_data = {}
        for key in settings.exchanges.keys():
            m = exchanges_data.get(key, {}).get(sym)
            if m:
                sym_data[key] = {
                    "price": m.price,
                    "funding_rate": m.funding_rate,
                    "volume_24h": m.volume_24h,
                    "native_interval": m.native_interval_hours,
                }
            else:
                sym_data[key] = None
        markets_display[sym] = sym_data
        
    opp_display = []
    for opp in opportunities:
        total_net_bps = opp.details.get("total_net_bps", 0)
        base_interval = opp.details.get("base_interval", 8)
        funding_bps = opp.details.get("funding_diff_scaled_bps", 0)
        net_spread_bps = opp.net_spread_bps
        
        funding_annual = funding_bps * (24 / base_interval) * 3.65 if base_interval > 0 else 0
        total_apr = funding_annual + (net_spread_bps / 100.0)
        
        opp_display.append({
            "symbol": opp.symbol,
            "direction": opp.direction,
            "spread_bps": opp.net_spread_bps,
            "total_net_bps": total_net_bps,
            "total_apr": total_apr,
            "base_interval": base_interval,
            "details": opp.details,
        })
    
    # Sort by APR descending
    opp_display.sort(key=lambda x: x["total_apr"], reverse=True)
        
    # Update latest_data
    latest_data["markets"] = markets_display
    latest_data["opportunities"] = opp_display
    latest_data["reasons"] = reasons
    latest_data["symbol_max_intervals"] = symbol_max_intervals
    from datetime import datetime
    latest_data["last_update"] = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Multi-Exchange Mode)"
    bump_data_version()
    
    return opportunities


async def update_data_loop():
    """Background task to fetch data and find opportunities."""
    from datetime import datetime
    while True:
        interval = settings.schedule.interval_seconds
        start_time = time.time()
        try:
            # Record update timestamp at the START to reduce perceived lag
            latest_data["last_update"] = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Multi-Exchange Mode)"
            
            # --- Batch Processing Optimization ---
            # Use asyncio.gather for parallel fetching while tracking health per collector
            async def fetch_with_health_tracking(key: str, collector):
                try:
                    res = await collector.fetch_markets(settings.tracked_symbols)
                    collector.last_fetch_time = time.time()
                    collector.last_error = None
                    return key, res or {}
                except Exception as e:
                    collector.last_error = str(e)
                    logger.error(f"Exchange {key} fetch error: {e}")
                    return key, {}

            all_keys = list(collectors_hub.keys())
            fetch_tasks = [
                fetch_with_health_tracking(key, collectors_hub[key])
                for key in all_keys
            ]
            results = await asyncio.gather(*fetch_tasks)
            
            for key, data in results:
                logger.info(f"Exchange {key} returned {len(data)} markets.")
                if data:
                    latest_data["raw_exchanges_data"][key] = data
                elif key not in latest_data["raw_exchanges_data"]:
                    latest_data["raw_exchanges_data"][key] = {}
                else:
                    logger.warning(
                        "Exchange %s returned no data; keeping previous snapshot with %d markets.",
                        key,
                        len(latest_data["raw_exchanges_data"].get(key, {})),
                    )
            
            # :55 快照 — 结算前 5 分钟内锁定当前费率
            update_pre_settlement_rates(latest_data["raw_exchanges_data"])
            # :01 结算 — 结算后 5 分钟内用快照费率记账
            accumulate_funding(latest_data["raw_exchanges_data"])
            
            # Trigger analysis and update state (analyze ALL data)
            opportunities = await perform_analysis()
            
            # Filter opportunities for WeChat notifications
            strict_opps = []
            min_spread = settings.thresholds.notify_min_spread_bps
            min_funding = settings.thresholds.notify_min_funding_bps
            min_total = settings.thresholds.notify_min_total_bps
            max_cover_hours = settings.thresholds.notify_max_cover_hours
            
            for opp in opportunities:
                spread_val = opp.net_spread_bps
                funding_val = opp.details.get("funding_diff_scaled_bps", 0)
                base_interval = opp.details.get("base_interval", 8)
                funding_daily_val = opp.details.get(
                    "funding_daily_bps",
                    funding_val * (24 / base_interval) if base_interval > 0 else 0,
                )
                projected_24h_val = opp.details.get(
                    "projected_24h_bps",
                    spread_val + funding_daily_val,
                )
                cover_hours = opp.details.get("cover_hours")
                
                # Settlement direction filter:
                # Block unfavorable direction where long side settles LESS frequently
                # than short side (e.g. 4h long : 1h short), because when funding
                # turns negative, the short side charges you every hour and you have
                # very little time to exit.
                # Only allow:
                #   1. Same interval (e.g. 8h:8h, 1h:1h)
                #   2. Favorable: long on short-interval, short on long-interval
                native_intervals = opp.details.get("native_intervals", {})
                buy_interval = native_intervals.get("buy", 8)
                sell_interval = native_intervals.get("sell", 8)
                # buy = long side, sell = short side
                if buy_interval > sell_interval:
                    # Unfavorable: long side settles less frequently than short side
                    continue
                
                aligned_opportunity = (
                    spread_val >= min_spread
                    and funding_daily_val >= min_funding
                    and projected_24h_val >= min_total
                )
                funding_cover_opportunity = (
                    spread_val < 0
                    and cover_hours is not None
                    and cover_hours <= max_cover_hours
                    and funding_daily_val >= min_funding
                    and projected_24h_val >= min_total
                )

                if aligned_opportunity or funding_cover_opportunity:
                    strict_opps.append(opp)
            
            if strict_opps:
                await notifier.send(strict_opps)
            
            # Check for position exit signals
            try:
                exit_signals = check_exit_signals(latest_data["raw_exchanges_data"], settings)
                if exit_signals:
                    await notifier.send_exit_alerts(exit_signals)
            except Exception as e:
                logger.error(f"Error checking exit signals: {e}")

            logger.info(f"Update successful. Last update: {latest_data['last_update']}")
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Update loop error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
        # Calculate remaining sleep time to maintain a stable 30s interval
        elapsed = time.time() - start_time
        sleep_time = max(0.1, interval - elapsed)
        await asyncio.sleep(sleep_time)

@app.get("/api/data", response_model=DashboardData)
async def get_dashboard_data():
    return latest_data

@app.get("/api/health")
async def get_health():
    """System health check endpoint."""
    collectors_status = {}
    for key, c in collectors_hub.items():
        collectors_status[key] = {
            "name": c.settings.name,
            "status": "online" if c.last_error is None else "error",
            "last_fetch": datetime.fromtimestamp(c.last_fetch_time).strftime('%Y-%m-%d %H:%M:%S') if c.last_fetch_time > 0 else "N/A",
            "error": c.last_error
        }
    
    return {
        "status": "healthy",
        "last_update": latest_data["last_update"],
        "collectors": collectors_status
    }

@app.get("/api/settings")
async def get_settings():
    return {
        "tracked_symbols": settings.tracked_symbols,
        "spread_threshold": settings.thresholds.min_spread_bps,
        "interval": settings.schedule.interval_seconds
    }

class ExchangeToggle(BaseModel):
    name: str
    enabled: bool

@app.get("/api/exchanges")
async def get_exchanges():
    """Return list of all available exchanges (client filters locally)."""
    return [{"name": k, "enabled": cfg.enabled} for k, cfg in settings.exchanges.items()]

# POST endpoint deprecated - preferences now stored client-side in localStorage
# Keeping for backward compatibility but it does nothing
@app.post("/api/exchanges")
async def toggle_exchange(item: ExchangeToggle):
    logger.info(f"Toggle request for {item.name} (client-side mode, no server action)")
    return {"status": "success", "name": item.name, "enabled": item.enabled, "note": "Preferences are now stored locally in your browser."}

# ===========================
# User / Position Management APIs (Auth Required)
# ===========================

class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=64)
    role: str = Field(default="trader")
    wecom_webhook: str = Field(default="", max_length=512)
    enabled: bool = True


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=64)
    wecom_webhook: Optional[str] = Field(default=None, max_length=512)
    role: Optional[str] = None
    enabled: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)


@app.get("/api/me")
async def get_me(current_user: Dict = Depends(get_current_user)):
    """Return current authenticated user info."""
    return current_user


@app.get("/api/users")
async def list_users(_: Dict = Depends(require_admin)):
    """List users (admin only)."""
    from db import get_all_users
    return get_all_users(include_disabled=True)


@app.post("/api/users")
async def create_user_endpoint(user: UserCreate, _: Dict = Depends(require_admin)):
    """Create user (admin only)."""
    from db import create_user
    _validate_username(user.username)
    _validate_role(user.role)
    try:
        result = create_user(
            username=user.username,
            password_hash=_hash_password(user.password),
            display_name=user.display_name,
            role=user.role,
            wecom_webhook=user.wecom_webhook,
            enabled=user.enabled,
        )
        logger.info("Admin created user: %s", user.username)
        return {"status": "success", "user": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/users/{user_id}")
async def update_user_endpoint(user_id: str, update: UserUpdate, _: Dict = Depends(require_admin)):
    """Update user (admin only)."""
    from db import get_all_users, get_user_by_id, update_user
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if update.role is not None:
        _validate_role(update.role)

    fields = {}
    if update.display_name is not None:
        fields["display_name"] = update.display_name
    if update.wecom_webhook is not None:
        fields["wecom_webhook"] = update.wecom_webhook
    if update.role is not None:
        fields["role"] = update.role
    if update.enabled is not None:
        fields["enabled"] = update.enabled
    if update.password:
        fields["password_hash"] = _hash_password(update.password)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    would_remove_admin = (
        target.get("role") == "admin"
        and (
            fields.get("role", target.get("role")) != "admin"
            or fields.get("enabled", bool(target.get("enabled"))) is False
        )
    )
    if would_remove_admin:
        enabled_admins = [
            user for user in get_all_users(include_disabled=True)
            if user.get("role") == "admin" and user.get("enabled") and user.get("id") != user_id
        ]
        if not enabled_admins:
            raise HTTPException(status_code=400, detail="Cannot disable or demote the last enabled admin")

    if not update_user(user_id, **fields):
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Admin updated user: %s", user_id)
    return {"status": "success"}


@app.delete("/api/users/{user_id}")
async def delete_user_endpoint(user_id: str, _: Dict = Depends(require_admin)):
    """Delete user and their positions (admin only)."""
    from db import delete_user, get_user_by_id
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["role"] == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin user")
    delete_user(user_id)
    logger.info("Admin deleted user: %s", user_id)
    return {"status": "success"}


class PositionCreate(BaseModel):
    symbol: str
    long_exchange: str
    short_exchange: str
    entry_funding_long: float = 0
    entry_funding_short: float = 0
    entry_qty: float = 0
    entry_price_long: float = 0
    entry_price_short: float = 0

@app.get("/api/positions")
async def get_positions(
    current_user: Dict = Depends(get_current_user),
    all_users: bool = Query(False, description="Admin: view all users' positions"),
    owner: Optional[str] = Query(None, description="Filter by owner_id"),
    status_filter: Optional[str] = Query(None, description="open/closed/all"),
):
    """Get positions. Traders see only their own; admins can see all."""
    from db import get_positions as db_get_positions

    if status_filter == "all":
        db_status = None
    elif status_filter == "closed":
        db_status = "closed"
    else:
        db_status = "open"

    if current_user["role"] == "admin" and all_users and owner:
        return db_get_positions(owner_id=owner, status=db_status)
    if current_user["role"] == "admin" and all_users:
        return db_get_positions(status=db_status)
    if owner and owner != current_user["id"]:
        raise HTTPException(status_code=403, detail="Cannot view other users' positions")
    return db_get_positions(owner_id=current_user["id"], status=db_status)

@app.post("/api/positions")
async def create_position(pos: PositionCreate, current_user: Dict = Depends(get_current_user)):
    """Add a new position tagged with the current user."""
    
    # Auto-fill entry funding rates: 单边独立判断，缺哪边补哪边
    exchanges_data = latest_data.get("raw_exchanges_data", {})

    entry_funding_long = pos.entry_funding_long
    entry_funding_short = pos.entry_funding_short
    
    if entry_funding_long == 0:
        long_ex_data = exchanges_data.get(pos.long_exchange.lower(), {})
        long_market = long_ex_data.get(pos.symbol.upper())
        if long_market and long_market.funding_rate is not None:
            hours = getattr(long_market, 'native_interval_hours', 8)
            entry_funding_long = long_market.funding_rate * (8 / hours) if hours > 0 else long_market.funding_rate
            
    if entry_funding_short == 0:
        short_ex_data = exchanges_data.get(pos.short_exchange.lower(), {})
        short_market = short_ex_data.get(pos.symbol.upper())
        if short_market and short_market.funding_rate is not None:
            hours = getattr(short_market, 'native_interval_hours', 8)
            entry_funding_short = short_market.funding_rate * (8 / hours) if hours > 0 else short_market.funding_rate

    direction = f"{pos.long_exchange.lower()}_long_{pos.short_exchange.lower()}_short"
    position = add_position(
        symbol=pos.symbol.upper(),
        direction=direction,
        long_exchange=pos.long_exchange,
        short_exchange=pos.short_exchange,
        entry_funding_long=entry_funding_long,
        entry_funding_short=entry_funding_short,
        entry_qty=pos.entry_qty,
        entry_price_long=pos.entry_price_long,
        entry_price_short=pos.entry_price_short,
        owner_id=current_user["id"],
    )
    logger.info("User %s added position: %s", current_user["username"], pos.symbol)
    return {"status": "success", "position": position}

@app.delete("/api/positions/{position_id}")
async def delete_position(position_id: str, current_user: Dict = Depends(get_current_user)):
    """Remove a position. Traders can only delete their own."""
    from db import get_position_by_id
    pos = get_position_by_id(position_id)
    if not pos:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
    if current_user["role"] != "admin" and pos.get("owner_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Cannot delete another user's position")
    remove_position(position_id)
    logger.info("User %s removed position: %s", current_user["username"], position_id)
    return {"status": "success", "position_id": position_id}


@app.get("/api/positions/pnl")
async def get_positions_with_pnl(
    current_user: Dict = Depends(get_current_user),
    all_users: bool = Query(False),
    owner: Optional[str] = Query(None),
):
    """Get positions with computed PnL. Traders see own; admins can see all."""
    from db import get_positions as db_get_positions

    if current_user["role"] == "admin" and all_users and owner:
        positions_list = db_get_positions(owner_id=owner, status="open")
    elif current_user["role"] == "admin" and all_users:
        positions_list = db_get_positions(status="open")
    elif owner and owner != current_user["id"]:
        raise HTTPException(status_code=403, detail="Cannot view other users' positions")
    else:
        positions_list = db_get_positions(owner_id=current_user["id"], status="open")

    exchanges_data = latest_data.get("raw_exchanges_data", {})
    result = {}

    for pos in positions_list:
        p_id = pos.get("id", "")
        long_ex = pos.get("long_exchange", "").lower()
        short_ex = pos.get("short_exchange", "").lower()
        symbol = pos.get("symbol", "").upper()

        # Get exchange-specific taker fees
        long_cfg = settings.exchanges.get(long_ex)
        short_cfg = settings.exchanges.get(short_ex)
        long_taker = getattr(long_cfg, 'taker_bps', 5.0) if long_cfg else 5.0
        short_taker = getattr(short_cfg, 'taker_bps', 5.0) if short_cfg else 5.0

        # Inject current funding rates from live market data
        pos_copy = pos.copy()
        long_market = exchanges_data.get(long_ex, {}).get(symbol)
        short_market = exchanges_data.get(short_ex, {}).get(symbol)

        if long_market:
            rate = getattr(long_market, 'funding_rate', None)
            interval = getattr(long_market, 'native_interval_hours', 8) or 8
            if rate is not None:
                pos_copy["current_funding_long"] = rate * (8 / interval)
            # Get current price for mark-to-market
            pos_copy["current_price_long"] = getattr(long_market, 'price', None)

        if short_market:
            rate = getattr(short_market, 'funding_rate', None)
            interval = getattr(short_market, 'native_interval_hours', 8) or 8
            if rate is not None:
                pos_copy["current_funding_short"] = rate * (8 / interval)
            pos_copy["current_price_short"] = getattr(short_market, 'price', None)

        # Compute PnL
        pnl = estimate_position_pnl(pos_copy, long_taker, short_taker)

        # Calculate mark-to-market unrealized PnL (price change since entry)
        mtm_pnl = 0.0
        entry_qty = pos.get("entry_qty", 0)
        entry_price_long = pos.get("entry_price_long", 0)
        entry_price_short = pos.get("entry_price_short", 0)
        cur_price_long = pos_copy.get("current_price_long")
        cur_price_short = pos_copy.get("current_price_short")

        if entry_qty and entry_price_long and entry_price_short and cur_price_long and cur_price_short:
            # Long leg: profit = (current - entry) * qty
            # Short leg: profit = (entry - current) * qty
            mtm_pnl = (cur_price_long - entry_price_long) * entry_qty + \
                       (entry_price_short - cur_price_short) * entry_qty

        # Build enriched position data
        enriched = {**pos}
        if pnl:
            enriched["pnl"] = pnl
            # Add combined PnL (funding + mark-to-market)
            enriched["pnl"]["mtm_pnl"] = round(mtm_pnl, 2)
            enriched["pnl"]["total_pnl"] = round(pnl["net_pnl"] + mtm_pnl, 2)
        else:
            enriched["pnl"] = {
                "notional": 0, "total_fees": 0, "funding_income": 0,
                "net_pnl": 0, "is_profitable": False, "hours_held": 0,
                "mtm_pnl": round(mtm_pnl, 2), "total_pnl": round(mtm_pnl, 2),
            }

        enriched["current_price_long"] = cur_price_long
        enriched["current_price_short"] = cur_price_short
        result[p_id] = enriched

    return result


@app.get("/api/positions/reversals")
async def get_reversals(
    current_user: Dict = Depends(get_current_user),
    all_users: bool = Query(False),
    owner: Optional[str] = Query(None),
):
    """Get positions currently in reversal/watch state."""
    from db import get_positions as db_get_positions

    if current_user["role"] == "admin" and all_users and owner:
        positions_list = db_get_positions(owner_id=owner, status="open")
    elif current_user["role"] == "admin" and all_users:
        positions_list = db_get_positions(status="open")
    elif owner and owner != current_user["id"]:
        raise HTTPException(status_code=403, detail="Cannot view other users' reversals")
    else:
        positions_list = db_get_positions(owner_id=current_user["id"], status="open")

    reversals = []
    for pos in positions_list:
        health = pos.get("health_status", "healthy")
        if health in ("reversal", "watch"):
            reversals.append({
                "id": pos.get("id"),
                "symbol": pos.get("symbol"),
                "direction": pos.get("direction"),
                "long_exchange": pos.get("long_exchange"),
                "short_exchange": pos.get("short_exchange"),
                "owner_id": pos.get("owner_id"),
                "health_status": health,
                "reversal_reason": pos.get("reversal_reason"),
                "last_signal_at": pos.get("last_signal_at"),
                "current_funding_long": pos.get("current_funding_long"),
                "current_funding_short": pos.get("current_funding_short"),
                "current_price_long": pos.get("current_price_long"),
                "current_price_short": pos.get("current_price_short"),
            })
    return reversals

# Serve Frontend Static Files
from fastapi.staticfiles import StaticFiles
current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, "dashboard", "dist")

if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8011)
