"""
Database Management Module for Bybit Downloader
Handles all data persistence operations:
- Parquet file read/write with caching
- Database integrity checks
- Market data directory management
"""
import os
import functools
import pandas as pd
from datetime import timezone
import time

# Configuration constants
MARKET_DATA_DIR = "./market_data"
LOGS_DIR = "./task_logs"
os.makedirs(MARKET_DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# =============================================================================
# Low-RAM Parquet Cache
# =============================================================================

@functools.lru_cache(maxsize=4)  # Holds max 4 DFs to protect old Mac RAM
def _load_parquet_cached(file_path: str, mtime: float) -> pd.DataFrame:
    """Load parquet file with LRU caching to minimize RAM usage."""
    return pd.read_parquet(file_path)


def load_task_data_cached(task, np_local_global=None):
    """
    Load cached candle data for a task, filtered by the task's time period.

    🔧 CRITICAL: Respects your original design:
    - Uses already-loaded candles from parquet (fast, no re-download)
    - Filters ONLY to the task's specific analysis period (start_date to end_date)
    - Minimizes data for faster recalculation

    Args:
        task: DownloadTask instance
        np_local_global: numpy module alias for thread safety

    Returns:
        Filtered DataFrame with candle data
    """
    if np_local_global is None:
        import numpy as np_local_global

    sym = task.symbols[0]
    path = symbol_timeframe_path(sym, task.timeframe)
    fp = os.path.join(path, "data.parquet")
    
    if not os.path.exists(fp):
        print(f"⚠️ [CACHE] No parquet file found for {sym} {task.timeframe}")
        return pd.DataFrame()

    mtime = os.path.getmtime(fp)
    df = _load_parquet_cached(fp, mtime).copy()

    # Guarantee timestamp is int64 milliseconds for safe searchsorted & math
    if 'timestamp' in df.columns:
        if df['timestamp'].dtype.name.startswith('datetime'):
            df['timestamp'] = (df['timestamp'].astype(np_local_global.int64) // 1_000_000).astype(np_local_global.int64)
        else:
            df['timestamp'] = df['timestamp'].astype(np_local_global.int64)

    # 🔧 FILTER by task's analysis period (start_date to end_date)
    # This respects your JSON design: each task has its own time window
    if task.start_date and task.end_date:
        start_ms = int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000)

        # Add buffer before start (pre_buffer_minutes) to capture events leading to signal
        buffer_ms = getattr(task, 'pre_buffer_minutes', 60) * 60 * 1000
        start_ms -= buffer_ms

        df_filtered = df[(df['timestamp'] >= start_ms) & (df['timestamp'] <= end_ms)]

        if df_filtered.empty:
            print(f"⚠️ [CACHE] No data in period {task.start_date} to {task.end_date} for {sym} {task.timeframe}")
        else:
            print(f"✅ [CACHE] Loaded {len(df_filtered)} candles (filtered from {len(df)}) for {sym} {task.timeframe}")

        return df_filtered

    return df


def clear_parquet_cache():
    """Clear the parquet cache."""
    _load_parquet_cached.cache_clear()


# =============================================================================
# Database Helpers
# =============================================================================

def symbol_timeframe_path(symbol, timeframe):
    """Return the folder path for a given symbol and timeframe."""
    return os.path.join(MARKET_DATA_DIR, symbol.replace("/", "_"), timeframe)


def write_parquet_batch(symbol, timeframe, df, overwrite=False, task=None):
    """
    Write a DataFrame to a Parquet file inside the symbol/timeframe folder.
    
    If overwrite=False and file exists, merge with existing data 
    (keeping latest by timestamp).
    
    Returns number of duplicate rows removed (if task provided, logs it).
    """
    path = symbol_timeframe_path(symbol, timeframe)
    os.makedirs(path, exist_ok=True)
    file_path = os.path.join(path, "data.parquet")
    removed = 0
    
    if os.path.exists(file_path) and not overwrite:
        existing = pd.read_parquet(file_path)
        before = len(existing) + len(df)
        combined = pd.concat([existing, df]).drop_duplicates("timestamp", keep="last")
        removed = before - len(combined)
        combined.sort_values("timestamp").to_parquet(file_path, compression="zstd")
        if task and removed > 0:
            task.add_log(f"Removed {removed} duplicate timestamps during merge")
    else:
        df.to_parquet(file_path, compression="zstd")
    
    return removed


def read_existing_range(symbol, timeframe):
    """Read the minimum and maximum timestamp from an existing Parquet file."""
    p = symbol_timeframe_path(symbol, timeframe)
    fp = os.path.join(p, "data.parquet")
    
    if not os.path.exists(fp):
        return None, None
    
    df = pd.read_parquet(fp)
    if df.empty:
        return None, None
    
    now_ts = int(time.time() * 1000)
    if df["timestamp"].max() > now_ts + 86400000 * 365 * 10:
        print(f"WARNING: {symbol} {timeframe} has timestamps far in the future.")
    
    min_ts = int(df["timestamp"].astype(int).min())
    max_ts = int(df["timestamp"].astype(int).max())
    return min_ts, max_ts


def get_database_info():
    """
    Walk the market_data folder and collect metadata about each Parquet file.
    Safely skips corrupted files and prints their paths for manual cleanup.
    
    Returns:
        dict with keys: size (total bytes), symbols (count), details (list of file info)
    """
    details, total_size, symbols = [], 0, set()
    corrupted_files = []
    
    for root, _, files in os.walk(MARKET_DATA_DIR):
        for f in files:
            if f == "data.parquet":
                fp = os.path.join(root, f)
                rel = os.path.relpath(root, MARKET_DATA_DIR).split(os.sep)
                if len(rel) == 2:
                    sym, tf = rel
                    symbols.add(sym)
                
                try:
                    total_size += os.path.getsize(fp)
                    df = pd.read_parquet(fp)
                    if not df.empty:
                        start = df["timestamp"].min()
                        end = df["timestamp"].max()
                        details.append({
                            "symbol": sym,
                            "timeframe": tf,
                            "start": pd.to_datetime(start, unit='ms'),
                            "end": pd.to_datetime(end, unit='ms'),
                            "candles": len(df),
                            "size": os.path.getsize(fp)
                        })
                except Exception as e:
                    corrupted_files.append(fp)
                    print(f"⚠️ Skipping corrupted file: {fp} ({e})")

    if corrupted_files:
        print("\n" + "="*60)
        print("⚠️ CORRUPTED PARQUET FILES DETECTED ⚠️")
        print("These files will cause crashes. Delete them and re-download:")
        for f in corrupted_files:
            folder = os.path.dirname(f)
            print(f"  🗑️ rm -rf '{folder}'")
        print("="*60 + "\n")

    return {"size": total_size, "symbols": len(symbols), "details": details}
