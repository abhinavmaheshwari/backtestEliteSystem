import pandas as pd
from abc import ABC, abstractmethod
import yfinance as yf
import time
import random
import logging

logger = logging.getLogger(__name__)

class DataFetcher(ABC):
    @abstractmethod
    def get_ohlcv(self, symbol: str, interval: str, period: str, retries: int = 3) -> pd.DataFrame:
        """Fetch OHLCV data for a single symbol."""
        pass

    @abstractmethod
    def get_batch_ohlcv(self, symbols: list[str], interval: str, period: str, retries: int = 3) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV data for multiple symbols simultaneously."""
        pass

    @abstractmethod
    def get_quote(self, symbol: str) -> dict:
        """Fetch current quote for a symbol."""
        pass


class YFinanceFetcher(DataFetcher):
    def _normalize_symbol(self, symbol: str) -> str:
        # yfinance requires .NS suffix; KiteConnect uses raw NSE symbol
        # also handle underscore to hyphen conversion commonly needed for yfinance
        sym = symbol.replace("_", "-")
        if sym.startswith("^"):
            return sym
        return f"{sym}.NS" if not sym.endswith(".NS") else sym

    def get_ohlcv(self, symbol: str, interval: str, period: str, retries: int = 3) -> pd.DataFrame:
        ns_sym = self._normalize_symbol(symbol)
        for attempt in range(retries):
            try:
                df = yf.download(ns_sym, interval=interval, period=period, progress=False, auto_adjust=True, threads=False)
                if df is not None and not df.empty:
                    # Flatten MultiIndex if it exists
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    # Reset index so 'Date' or 'Datetime' is a column
                    df = df.reset_index().copy()
                    return df
            except Exception as e:
                logger.warning(f"⚠️ Single fetch failed for {ns_sym} (Attempt {attempt+1}/{retries}): {e}")
                wait = (2 ** attempt) * random.uniform(0.5, 1.5)
                time.sleep(wait)
        logger.error(f"❌ Exhausted retries fetching {symbol}")
        return None

    def get_batch_ohlcv(self, symbols: list[str], interval: str, period: str, retries: int = 3) -> dict[str, pd.DataFrame]:
        normalized_map = {self._normalize_symbol(s): s for s in symbols}
        tickers_str = " ".join(normalized_map.keys())
        
        for attempt in range(retries):
            try:
                raw = yf.download(tickers_str, period=period, interval=interval, progress=False, auto_adjust=True, threads=False, group_by="ticker")
                
                if raw is None or raw.empty:
                    raise ValueError("Empty dataframe returned by yfinance")
                
                if not isinstance(raw.columns, pd.MultiIndex) and len(symbols) > 1:
                    raise ValueError("yfinance returned flat DF instead of MultiIndex for batch")
                
                all_data = {}
                if isinstance(raw.columns, pd.MultiIndex):
                    level0 = raw.columns.get_level_values(0)
                    for ns_sym, raw_sym in normalized_map.items():
                        if ns_sym in level0:
                            sym_df = raw[ns_sym].dropna(how='all').reset_index().copy()
                            if not sym_df.empty:
                                all_data[raw_sym] = sym_df
                else:
                    sym_df = raw.dropna(how='all').reset_index().copy()
                    if not sym_df.empty:
                        all_data[symbols[0]] = sym_df
                
                return all_data
            except Exception as e:
                logger.warning(f"⚠️ Batch download error (Attempt {attempt+1}/{retries}): {e}")
                wait = (2 ** attempt) * random.uniform(0.5, 1.5)
                time.sleep(wait)
        
        logger.critical(f"❌ Batch fetch completely failed for {len(symbols)} symbols after {retries} retries.")
        return {}

    def get_quote(self, symbol: str) -> dict:
        ns_sym = self._normalize_symbol(symbol)
        try:
            ticker = yf.Ticker(ns_sym)
            return ticker.info
        except Exception as e:
            logger.error(f"Failed to fetch quote for {symbol}: {e}")
            return {}

# ── Factory ─────────────────────────────────────────────────────────────────

def get_fetcher() -> DataFetcher:
    from config import DATA_PROVIDER
    if DATA_PROVIDER == "kite":
        from data_providers.kite_fetcher import KiteFetcher
        return KiteFetcher()
    return YFinanceFetcher()
