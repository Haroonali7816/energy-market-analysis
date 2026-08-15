"""
SMARD API ingestion client.

Handles the two-step SMARD access pattern:
1. index endpoint -> list of available chunk timestamps
2. timeseries endpoint -> actual data for one chunk

"""

import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass

import requests

logging.basicConfig(level=logging.INFO, format = "%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("smard_client")

BASE_URL = "https://www.smard.de/app/chart_data"

GENERATION_FILTERS = {
    "4067": {"label": "wind_onshore", "is_renewable":True},
    "1225": {"label": "wind_offshore", "is_renewable":True},
    "4068": {"label": "solar", "is_renewable": True},
    "4066": {"label": "biomass", "is_renewable": True},
    "1226": {"label": "hydro", "is_renewable": True},
    "1223": {"label": "lignite", "is_renewable": False},
    "4069": {"label": "hard_coal", "is_renewable": False},
    "4071": {"label": "natural_gas", "is_renewable": False},
    "1224": {"label": "nuclear", "is_renewable": False},
}

LOAD_FILTER = {"410": {"label":"load_total", "region":"DE"}}
PRICE_FILTER = {"4169": {"label": "day_ahead_price", "region": "DE-LU"}}

RESOLUTION = "hour"
RAW_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

class SmardApiError(Exception):
    pass

@dataclass
class FetchResult:
    filter_id: str
    region: str
    chunks_found: int
    chunks_fetched: int
    chunks_failed: int

class SmardClient:
    def __init__(self,max_retries: int = 3, backoff_seconds:float= 2.0,request_delay: float = 0.3):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "energy-market-analytics/0.1 (portfolio project)"})
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.requests_delay = request_delay

    def _get_with_retry(self, url: str) -> dict | None:
        last_exception = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=30)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                last_exception = e
                wait = self.backoff_seconds * (2 ** (attempt - 1))
                logger.warning(f"Attempt {attempt}/ {self.max_retries} failed for {url}: {e}. Retrying in {wait}s.")
                time.sleep(wait)
        raise SmardApiError(f"Failed after {self.max_retries} attempts: {url}") from last_exception

    def get_available_timestamps(self,filter_id: str, region: str, resolution: str = RESOLUTION) -> list[int]:
        url = f"{BASE_URL}/{filter_id}/{region}/index_{resolution}.json"
        data = self._get_with_retry(url)
        if data is None:
            logger.warning(f"No data found for filter_id={filter_id}, region={region}.")
            return []
        return data.get("timestamps", [])

    def get_chunk(self, filter_id: str, region: str, timestamp: int, resolution: str = RESOLUTION) -> dict | None:
        url = f"{BASE_URL}/{filter_id}/{region}/{filter_id}_{region}_{resolution}_{timestamp}.json"
        return self._get_with_retry(url)

    def fetch_and_persist(self, filter_id: str, region: str, label: str, resolution: str = RESOLUTION) -> FetchResult:
         out_dir = RAW_DATA_DIR / label / region
         out_dir.mkdir(parents=True, exist_ok=True)
 
         timestamps = self.get_available_timestamps(filter_id, region, resolution)
         logger.info(f"[{label}/{region}] {len(timestamps)} chunks available")
         fetched, failed = 0, 0
         for ts in timestamps:
            out_path = out_dir / f"{ts}.json"
            if out_path.exists():
                continue
            try:
                chunk = self.get_chunk(filter_id, region, ts, resolution)
                if chunk is None:
                    logger.warning(f"[{label}/{region}] chunk {ts} returned 404, skipping")
                    failed += 1
                    continue
                with open(out_path, "w") as f:
                    json.dump(chunk, f)
                fetched += 1
            except SmardApiError as e:
                logger.error(f"[{label}/{region}] chunk {ts} failed permanently: {e}")
                failed += 1
 
            time.sleep(self.requests_delay)
         return FetchResult(filter_id, region, len(timestamps), fetched, failed)
def run_full_ingestion():
    client = SmardClient()
    results = []
 
    logger.info("=== Generation data (9 energy sources, region DE) ===")
    for filter_id, meta in GENERATION_FILTERS.items():
        result = client.fetch_and_persist(filter_id, region="DE", label=f"generation_{meta['label']}")
        results.append(result)
 
    logger.info("=== Load data ===")
    for filter_id, meta in LOAD_FILTER.items():
        result = client.fetch_and_persist(filter_id, region=meta["region"], label=meta["label"])
        results.append(result)
 
    logger.info("=== Price data (region DE-LU, not DE) ===")
    for filter_id, meta in PRICE_FILTER.items():
        result = client.fetch_and_persist(filter_id, region=meta["region"], label=meta["label"])
        results.append(result)
 
    logger.info("=== Ingestion summary ===")
    total_fetched, total_failed = 0, 0
    for r in results:
        logger.info(f"  filter={r.filter_id} region={r.region}: {r.chunks_fetched} fetched, {r.chunks_failed} failed, {r.chunks_found} total available")
        total_fetched += r.chunks_fetched
        total_failed += r.chunks_failed
    logger.info(f"TOTAL: {total_fetched} chunks fetched, {total_failed} failed")
 
 
if __name__ == "__main__":
    run_full_ingestion()