# app/data_registry.py
# Centralized registry defining datasets and their fetch cadence (seconds)
from datetime import timedelta

DATASETS = {
    "price_1m": {
        "cadence": 60,            # seconds
        "period": "1mo",
        "resolution": "1m",
        "storage": "parquet",
        "min_rows": 50,
    },
    "price_15m": {
        "cadence": 900,
        "period": "6mo",
        "resolution": "15m",
        "storage": "parquet",
    },
    "price_1d": {
        "cadence": 24 * 3600,     # daily
        "period": "1y",
        "resolution": "1d",
        "storage": "parquet",
    },
    "promoter_pledge": {
        "cadence": 24 * 3600,
        "storage": "json",
    },
    "fundamentals_quarterly": {
        "cadence": 90 * 24 * 3600,
        "storage": "json",
    },
    "company_profile": {
        "cadence": 30 * 24 * 3600,
        "storage": "json",
    },
}

