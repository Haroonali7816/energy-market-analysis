import json
import logging
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import select

from models import (
    get_engine, init_db, DimDatetime, DimRegion, DimEnergySource, FactGeneration, FactMarket,
    )
from smard_client import GENERATION_FILTERS, RAW_DATA_DIR

logging.basicConfig(level=logging.INFO, format = "%(asctime)s [%(levelname)s] % (message)s")
logger = logging.getLogger("loader")

REGION_CODE = "DE"
REGION_NAME = "Germany (national)"

def load_chunks_as_dataframe(label: str, region:str) -> pd.DataFrame:
    chunk_dir = RAW_DATA_DIR / label / region
    if not chunk_dir.exists():
        logger.warning(f"No raw data directory for {label}/{region}")
        return pd.DataFrame(columns=["timestamp_ms", "value"])
    rows = []
    for chunk_file in chunk_dir.glob("*.json"):
        with open(chunk_file) as f:
            data = json.load(f)
        for ts, value in data.get("series", []):
            if value is not None:
                rows.append((ts,value))
    if not rows:
        return pd.DataFrame(columns=["timestamp_ms", "value"])
    df = pd.DataFrame(rows, columns=["timestamp_ms","value"])
    df = df.drop_duplicates(subset="timestamp_ms", keep="last").sort_values("timestamp_ms")
    return df

def derive_datetime_fields(timestamp_ms: int) -> dict:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return {
        "timestamp_utc": dt.replace(tzinfo=None),
        "date": dt.date(),
        "hour": dt.hour,
        "day_of_week": dt.weekday(),
        "is_weekend": dt.weekday() >= 5,
        "month": dt.month,
        "quarter": (dt.month - 1) // 3 + 1,
        "year": dt.year,
    }

def build_dim_datetime(session: Session, all_timestamps: set[int]) -> dict[int, int]:
    existing_rows = session.execute(select(DimDatetime.datetime_id,DimDatetime.timestamp_utc)).all()
    existing_ts_to_id = {
        int(row.timestamp_utc.replace(tzinfo=timezone.utc).timestamp() * 1000): row.datetime_id
        for row in existing_rows
    }

    new_timestamps = sorted(ts for ts in all_timestamps if ts not in existing_ts_to_id)
    logger.info(f"dim_datetime: {len(existing_ts_to_id)} existing, {len(new_timestamps)} new to insert")

    if new_timestamps:
        new_rows = [derive_datetime_fields(ts) for ts in new_timestamps]
        session.bulk_insert_mappings(DimDatetime, new_rows)
        session.commit()

        refreshed = session.execute(select(DimDatetime.datetime_id, DimDatetime.timestamp_utc)).all()
        existing_ts_to_id = {
            int(row.timestamp_utc.replace(tzinfo=timezone.utc).timestamp() * 1000): row.datetime_id
            for row in refreshed
        }
 
    return existing_ts_to_id

def get_or_create_region(session: Session) -> int:
    region = session.execute(select(DimRegion).where(DimRegion.region_code == REGION_CODE)).scalar_one_or_none()
    if region is None:
        region = DimRegion(region_code=REGION_CODE, region_name=REGION_NAME)
        session.add(region)
        session.commit()
        logger.info(f"Created dim_region row: {REGION_CODE}")
    return region.region_id


def get_or_create_energy_sources(session: Session) -> dict[str, int]:
    """Returns label -> energy_source_id, creating rows as needed."""
    label_to_id = {}
    for filter_id, meta in GENERATION_FILTERS.items():
        source = session.execute(
            select(DimEnergySource).where(DimEnergySource.source_code == meta["label"])
        ).scalar_one_or_none()
        if source is None:
            source = DimEnergySource(
                source_code=meta["label"],
                source_name=meta["label"].replace("_", " ").title(),
                is_renewable=meta["is_renewable"],
            )
            session.add(source)
            session.commit()
        label_to_id[meta["label"]] = source.energy_source_id
    return label_to_id


def load_generation_facts(session: Session, region_id: int, ts_to_datetime_id: dict[int, int],
                           energy_source_ids: dict[str, int]) -> int:
    total_inserted = 0
    for filter_id, meta in GENERATION_FILTERS.items():
        label = meta["label"]
        df = load_chunks_as_dataframe(f"generation_{label}", "DE")
        if df.empty:
            continue
 
        source_id = energy_source_ids[label]
        rows = [
            {
                "datetime_id": ts_to_datetime_id[int(ts)],
                "region_id": region_id,
                "energy_source_id": source_id,
                "generation_mwh": float(value),
            }
            for ts, value in zip(df["timestamp_ms"], df["value"])
            if int(ts) in ts_to_datetime_id
        ] 
        inserted = _bulk_insert_ignore_duplicates(session, FactGeneration, rows)
        logger.info(f"fact_generation[{label}]: {inserted}/{len(rows)} inserted (rest already existed)")
        total_inserted += inserted
 
    return total_inserted

def load_market_facts(session: Session, region_id: int, ts_to_datetime_id: dict[int, int]) -> int:
    price_df = load_chunks_as_dataframe("day_ahead_price", "DE-LU")
    load_df = load_chunks_as_dataframe("load_total", "DE")
 
    price_map = dict(zip(price_df["timestamp_ms"].astype(int), price_df["value"]))
    load_map = dict(zip(load_df["timestamp_ms"].astype(int), load_df["value"]))
 
    all_ts = set(price_map) | set(load_map)
    rows = [
        {
            "datetime_id": ts_to_datetime_id[ts],
            "region_id": region_id,
            "price_eur_mwh": price_map.get(ts),
            "load_mwh": load_map.get(ts),
        }
        for ts in all_ts
        if ts in ts_to_datetime_id and price_map.get(ts) is not None  # price is the required measure
    ]
 
    inserted = _bulk_insert_ignore_duplicates(session, FactMarket, rows)
    logger.info(f"fact_market: {inserted}/{len(rows)} inserted (rest already existed)")
    return inserted
 
 
def _bulk_insert_ignore_duplicates(session: Session, model, rows: list[dict]) -> int:
   
    if not rows:
        return 0
    table = model.__table__
    stmt = table.insert().prefix_with("OR IGNORE")  # SQLite-specific; see note below for Postgres
    result = session.execute(stmt, rows)
    session.commit()
    return result.rowcount
 
 
def run_load():
    engine = get_engine()
    init_db(engine)
 
    with Session(engine) as session:
        region_id = get_or_create_region(session)
        energy_source_ids = get_or_create_energy_sources(session)
 
        logger.info("Scanning raw chunks to collect all timestamps needing dim_datetime rows...")
        all_timestamps = set()
        for filter_id, meta in GENERATION_FILTERS.items():
            df = load_chunks_as_dataframe(f"generation_{meta['label']}", "DE")
            all_timestamps.update(int(ts) for ts in df["timestamp_ms"])
        all_timestamps.update(int(ts) for ts in load_chunks_as_dataframe("load_total", "DE")["timestamp_ms"])
        all_timestamps.update(int(ts) for ts in load_chunks_as_dataframe("day_ahead_price", "DE-LU")["timestamp_ms"])
        logger.info(f"Found {len(all_timestamps)} unique timestamps across all series")
 
        ts_to_datetime_id = build_dim_datetime(session, all_timestamps)
 
        gen_inserted = load_generation_facts(session, region_id, ts_to_datetime_id, energy_source_ids)
        market_inserted = load_market_facts(session, region_id, ts_to_datetime_id)
 
        logger.info(f"DONE. fact_generation rows inserted: {gen_inserted}, fact_market rows inserted: {market_inserted}")
 
 
if __name__ == "__main__":
    run_load()