"""
Wethr configuration — all settings in one place.

Override via environment variables with WETHR_ prefix:
    WETHR_MIN_EDGE=0.10  →  MIN_EDGE_THRESHOLD = 0.10
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
COLLECTOR_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = COLLECTOR_ROOT.parent

DATA_DIR = Path(os.getenv("WETHR_DATA_DIR", str(REPO_ROOT / "data"))).expanduser()
DB_PATH = Path(os.getenv("WETHR_DB_PATH", str(DATA_DIR / "wethr.db"))).expanduser()

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# How long a writer waits for the SQLite lock before giving up. The collector
# loop and the calibration timers write to the same file.
DB_BUSY_TIMEOUT_S = float(os.getenv("WETHR_DB_BUSY_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Gamma API (Polymarket market discovery)
# ---------------------------------------------------------------------------
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
GAMMA_EVENTS_URL = f"{GAMMA_API_BASE}/events"
GAMMA_MARKETS_URL = f"{GAMMA_API_BASE}/markets"

# ---------------------------------------------------------------------------
# Polymarket CLOB API
# ---------------------------------------------------------------------------
CLOB_API_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# ---------------------------------------------------------------------------
# Open-Meteo Ensemble API
# ---------------------------------------------------------------------------
ENSEMBLE_API_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_API_BASE = "https://api.open-meteo.com/v1/forecast"

# Ensemble models to fetch
# Open-Meteo omits the control run, so actual counts are N-1 from docs.
# GFS ensemble model string is unstable — we probe multiple candidates.
ENSEMBLE_MODELS = [
    "ecmwf_ifs025_ensemble",   # 50 members — confirmed working
    "icon_seamless_eps",       # 39 members — confirmed working
    "gem_global_ensemble",     # 20 members — confirmed working
]

# GFS ensemble: the API model string has changed multiple times.
# We try these in order until one succeeds.
GFS_ENSEMBLE_CANDIDATES = [
    "gfs_seamless_eps",        # Original documented name
    "gfs025_eps",              # Alternate
    "gfs05_eps",               # 0.5° version
    "ncep_gefs025",            # Internal domain name
    "gefs025",                 # Short form
]

# Member counts per model — what Open-Meteo actually returns
MODEL_MEMBER_COUNTS = {
    "ecmwf_ifs025_ensemble": 50,
    "icon_seamless_eps": 39,
    "gem_global_ensemble": 20,
    # GFS added dynamically after probe
}

ENSEMBLE_FORECAST_DAYS = 10  # How far ahead to fetch

# ---------------------------------------------------------------------------
# City / Station Configuration
# ---------------------------------------------------------------------------
# Coordinates match ASOS stations that Polymarket/Weather Underground use.
# temp_unit: the unit Polymarket brackets use for this city's markets.

@dataclass(frozen=True)
class CityConfig:
    name: str              # Display name
    slug: str              # URL-friendly / key
    station: str           # ASOS/ICAO station ID
    lat: float
    lon: float
    timezone: str
    temp_unit: str         # "F" or "C" — what Polymarket brackets use
    gamma_tag: str = ""    # Search term for Gamma API
    resolution_adapter: str = "auto"  # nws, metar, or a dedicated official adapter
    precision: float = 1.0        # Market rounding increment

CITIES: dict[str, CityConfig] = {
    "nyc": CityConfig(
        name="New York City", slug="nyc", station="KLGA",
        lat=40.77, lon=-73.87, timezone="America/New_York",
        temp_unit="F", gamma_tag="NYC",
    ),
    "chicago": CityConfig(
        name="Chicago", slug="chicago", station="KORD",
        lat=41.98, lon=-87.90, timezone="America/Chicago",
        temp_unit="F", gamma_tag="Chicago",
    ),
    "miami": CityConfig(
        name="Miami", slug="miami", station="KMIA",
        lat=25.79, lon=-80.29, timezone="America/New_York",
        temp_unit="F", gamma_tag="Miami",
    ),
    "los_angeles": CityConfig(
        name="Los Angeles", slug="los_angeles", station="KLAX",
        lat=33.94, lon=-118.41, timezone="America/Los_Angeles",
        temp_unit="F", gamma_tag="Los Angeles",
    ),
    "denver": CityConfig(
        name="Denver", slug="denver", station="KDEN",
        lat=39.85, lon=-104.66, timezone="America/Denver",
        temp_unit="F", gamma_tag="Denver",
    ),
    "london": CityConfig(
        name="London", slug="london", station="EGLC",
        lat=51.51, lon=0.05, timezone="Europe/London",
        temp_unit="C", gamma_tag="London",
    ),
    "seoul": CityConfig(
        name="Seoul", slug="seoul", station="RKSI",
        lat=37.46, lon=126.44, timezone="Asia/Seoul",
        temp_unit="C", gamma_tag="Seoul",
    ),
    "toronto": CityConfig(
        name="Toronto", slug="toronto", station="CYYZ",
        lat=43.68, lon=-79.63, timezone="America/Toronto",
        temp_unit="C", gamma_tag="Toronto",
    ),
    "atlanta": CityConfig(
        name="Atlanta", slug="atlanta", station="KATL",
        lat=33.64, lon=-84.43, timezone="America/New_York",
        temp_unit="F", gamma_tag="Atlanta",
    ),
    "dallas": CityConfig(
        name="Dallas", slug="dallas", station="KDFW",
        lat=32.90, lon=-97.04, timezone="America/Chicago",
        temp_unit="F", gamma_tag="Dallas",
    ),
    "seattle": CityConfig(
        name="Seattle", slug="seattle", station="KSEA",
        lat=47.45, lon=-122.31, timezone="America/Los_Angeles",
        temp_unit="F", gamma_tag="Seattle",
    ),
    # --- New cities seen on Polymarket ---
    "tel_aviv": CityConfig(
        name="Tel Aviv", slug="tel_aviv", station="LLBG",
        lat=32.01, lon=34.88, timezone="Asia/Jerusalem",
        temp_unit="C", gamma_tag="Tel Aviv",
    ),
    "paris": CityConfig(
        name="Paris", slug="paris", station="LFPG",
        lat=49.01, lon=2.55, timezone="Europe/Paris",
        temp_unit="C", gamma_tag="Paris",
    ),
    "sao_paulo": CityConfig(
        name="São Paulo", slug="sao_paulo", station="SBGR",
        lat=-23.43, lon=-46.47, timezone="America/Sao_Paulo",
        temp_unit="C", gamma_tag="Sao Paulo",
    ),
    "buenos_aires": CityConfig(
        name="Buenos Aires", slug="buenos_aires", station="SAEZ",
        lat=-34.82, lon=-58.54, timezone="America/Argentina/Buenos_Aires",
        temp_unit="C", gamma_tag="Buenos Aires",
    ),
}

# ---------------------------------------------------------------------------
# Trading Parameters
# ---------------------------------------------------------------------------
MIN_EDGE_THRESHOLD = float(os.getenv("WETHR_MIN_EDGE", "0.08"))     # 8%
MIN_ENTRY_PRICE = float(os.getenv("WETHR_MIN_ENTRY", "0.05"))       # 5c — below this, no real liquidity
MAX_ENTRY_PRICE = float(os.getenv("WETHR_MAX_ENTRY", "0.50"))       # 50c — above this, risk/reward is poor
MAX_TRADE_SIZE_USD = float(os.getenv("WETHR_MAX_TRADE", "100.0"))   # $100 per trade
KELLY_FRACTION = float(os.getenv("WETHR_KELLY_FRAC", "0.05"))       # 5% fractional Kelly
MAX_BANKROLL_PCT = float(os.getenv("WETHR_MAX_BANK_PCT", "0.05"))   # 5% max per trade
INITIAL_BANKROLL = float(os.getenv("WETHR_BANKROLL", "10000.0"))     # $10K paper
DAILY_LOSS_LIMIT = float(os.getenv("WETHR_DAILY_LOSS", "300.0"))    # Circuit breaker
MAX_PENDING_TRADES = int(os.getenv("WETHR_MAX_PENDING", "20"))

# ---------------------------------------------------------------------------
# Scan Intervals
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SECONDS = int(os.getenv("WETHR_SCAN_INTERVAL", "600"))  # 5 min

# ---------------------------------------------------------------------------
# Ensemble confidence threshold
# ---------------------------------------------------------------------------
# Minimum ensemble agreement to consider a signal reliable.
# e.g., 0.65 means 65%+ of members must agree on the direction.
MIN_ENSEMBLE_CONFIDENCE = float(os.getenv("WETHR_MIN_CONFIDENCE", "0.60"))

# ---------------------------------------------------------------------------
# HTTP settings
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 30  # seconds
HTTP_RETRIES = 3
USER_AGENT = "wethr-agent/0.1"

# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = (
    os.getenv("WETHR_TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN", "")
)
TELEGRAM_CHAT_ID = (
    os.getenv("WETHR_TELEGRAM_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID", "")
)
TELEGRAM_MESSAGE_THREAD_ID = os.getenv("WETHR_TELEGRAM_MESSAGE_THREAD_ID", "")
