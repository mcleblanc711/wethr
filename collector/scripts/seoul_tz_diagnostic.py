"""
Seoul timezone diagnostic: compare Open-Meteo daily max aggregation methods.

Fetches hourly + daily data for Seoul from ECMWF ensemble and compares:
1. Daily max from Open-Meteo with timezone=Asia/Seoul
2. Daily max from Open-Meteo without timezone param (UTC default)
3. Daily max computed from hourly data using UTC boundaries
4. Daily max computed from hourly data using KST boundaries
"""
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np

SEOUL_LAT = 37.46
SEOUL_LON = 126.44
MODEL = "ecmwf_ifs025_ensemble"
KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")

# Use recent past dates for historical/forecast data
# Open-Meteo ensemble gives forecasts, so use dates from the past few days
START = "2026-03-28"
END = "2026-04-01"


async def fetch(client: httpx.AsyncClient, url: str, label: str) -> dict | None:
    print(f"  Fetching {label}...")
    resp = await client.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


async def main():
    base = "https://ensemble-api.open-meteo.com/v1/ensemble"

    # URL 1: daily max WITH timezone=Asia/Seoul
    url_tz = (
        f"{base}?latitude={SEOUL_LAT}&longitude={SEOUL_LON}"
        f"&daily=temperature_2m_max&models={MODEL}"
        f"&temperature_unit=celsius&timezone=Asia/Seoul"
        f"&start_date={START}&end_date={END}"
    )

    # URL 2: daily max WITHOUT timezone (UTC default)
    url_utc = (
        f"{base}?latitude={SEOUL_LAT}&longitude={SEOUL_LON}"
        f"&daily=temperature_2m_max&models={MODEL}"
        f"&temperature_unit=celsius"
        f"&start_date={START}&end_date={END}"
    )

    # URL 3: hourly temperature_2m
    url_hourly = (
        f"{base}?latitude={SEOUL_LAT}&longitude={SEOUL_LON}"
        f"&hourly=temperature_2m&models={MODEL}"
        f"&temperature_unit=celsius"
        f"&start_date={START}&end_date={END}"
    )

    async with httpx.AsyncClient() as client:
        data_tz = await fetch(client, url_tz, "daily with tz=Asia/Seoul")
        await asyncio.sleep(2)
        data_utc = await fetch(client, url_utc, "daily without tz (UTC)")
        await asyncio.sleep(2)
        data_hourly = await fetch(client, url_hourly, "hourly data")

    # --- Parse daily results ---
    daily_tz = data_tz["daily"]
    daily_utc = data_utc["daily"]

    # Average across members for each date
    tz_dates = daily_tz["time"]
    utc_dates = daily_utc["time"]

    tz_member_keys = sorted(k for k in daily_tz if k.startswith("temperature_2m_max_member"))
    utc_member_keys = sorted(k for k in daily_utc if k.startswith("temperature_2m_max_member"))

    print(f"\n  Members found: {len(tz_member_keys)} (tz), {len(utc_member_keys)} (utc)")

    # Compute mean across members per date
    def mean_per_date(daily_data, member_keys, dates):
        result = {}
        for i, d in enumerate(dates):
            vals = [daily_data[k][i] for k in member_keys if daily_data[k][i] is not None]
            result[d] = np.mean(vals) if vals else float('nan')
        return result

    tz_means = mean_per_date(daily_tz, tz_member_keys, tz_dates)
    utc_means = mean_per_date(daily_utc, utc_member_keys, utc_dates)

    # --- Parse hourly results, compute daily max two ways ---
    hourly = data_hourly["hourly"]
    hourly_times = hourly["time"]  # ISO strings like "2026-03-28T00:00"
    hourly_member_keys = sorted(k for k in hourly if k.startswith("temperature_2m_member"))
    print(f"  Hourly members: {len(hourly_member_keys)}")

    # Build per-member hourly arrays
    # hourly times are in UTC (no timezone param)
    member_hourly = {}  # member_key -> [(utc_dt, temp_c), ...]
    for mk in hourly_member_keys:
        vals = hourly[mk]
        series = []
        for i, t_str in enumerate(hourly_times):
            if vals[i] is not None:
                dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=UTC)
                series.append((dt_utc, vals[i]))
        member_hourly[mk] = series

    # Compute daily max per member using UTC boundaries
    def daily_max_by_tz(member_series, tz):
        """Group hourly temps by local date, return max per date."""
        by_date = {}
        for dt_utc, temp in member_series:
            local_dt = dt_utc.astimezone(tz)
            local_date = local_dt.date().isoformat()
            by_date.setdefault(local_date, []).append(temp)
        return {d: max(temps) for d, temps in by_date.items()}

    # Compute mean of per-member daily max
    def mean_daily_max(member_hourly_dict, tz):
        all_dates = set()
        member_maxes = {}
        for mk, series in member_hourly_dict.items():
            dm = daily_max_by_tz(series, tz)
            member_maxes[mk] = dm
            all_dates.update(dm.keys())

        result = {}
        for d in sorted(all_dates):
            vals = [member_maxes[mk][d] for mk in member_maxes if d in member_maxes[mk]]
            result[d] = np.mean(vals) if vals else float('nan')
        return result

    hourly_utc_max = mean_daily_max(member_hourly, UTC)
    hourly_kst_max = mean_daily_max(member_hourly, KST)

    # --- Print comparison table ---
    all_dates = sorted(set(tz_dates) | set(utc_dates) | set(hourly_utc_max.keys()) | set(hourly_kst_max.keys()))

    print(f"\n{'Date':<12} {'API tz=Seoul':>12} {'API no tz':>12} {'Hourly→UTC':>12} {'Hourly→KST':>12} {'Δ(Seoul-KST)':>13}")
    print("-" * 75)

    for d in all_dates:
        v_tz = tz_means.get(d, float('nan'))
        v_utc = utc_means.get(d, float('nan'))
        v_h_utc = hourly_utc_max.get(d, float('nan'))
        v_h_kst = hourly_kst_max.get(d, float('nan'))
        delta = v_tz - v_h_kst if not (np.isnan(v_tz) or np.isnan(v_h_kst)) else float('nan')
        print(f"{d:<12} {v_tz:>12.2f} {v_utc:>12.2f} {v_h_utc:>12.2f} {v_h_kst:>12.2f} {delta:>+13.2f}")


if __name__ == "__main__":
    asyncio.run(main())
