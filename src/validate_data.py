# Data Validation for the German Market Analytics star schema.

import logging
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from models import get_engine,DimDatetime, DimRegion, DimEnergySource,FactGeneration, FactMarket

logging.basicConfig(level=logging.INFO, format = "%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("validate")

REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "data_validation_report.md"

# Germany's last three nuclear plants shut down on April 15, 2023. This is the last date for which we expect to see nuclear generation data in the SMARD data.
NUCLEAR_SHUTDOWN_DATE = date(2023, 4, 15)

#SMARD's own historical price range rarely exceeds roughly +/- 500 EUR/Mwh

PRICE_REVIEW_LOW = -500.0
PRICE_REVIEW_HIGH = 500.0

class ValidationCheck:
    def __init__(self,name: str, passed: bool, detail: str):
        self.name = name
        self.passed = passed
        self.detail = detail

    def to_markdown(self) -> str:
        status = "PASS" if self.passed else "FLAGGED"
        return f"### [{status}] {self.name}\n{self.detail}\n"

def check_row_counts(session: Session) -> ValidationCheck:
    counts = {
        "dim_datetime": session.execute(select(func.count()).select_from(DimDatetime)).scalar(),
        "dim_region": session.execute(select(func.count()).select_from(DimRegion)).scalar(),
        "dim_energy_source": session.execute(select(func.count()).select_from(DimEnergySource)).scalar(),
        "fact_generation": session.execute(select(func.count()).select_from(FactGeneration)).scalar(),
        "fact_market": session.execute(select(func.count()).select_from(FactMarket)).scalar()
    }
    detail = "\n".join(f"- {table}: {count:,} rows" for table,count in counts.items())

    passed = counts["fact_generation"] > 100_000 and counts["fact_market"] > 10_000
    return ValidationCheck("Row counts", passed, detail)

def check_hourly_coverage_gaps(session: Session) -> ValidationCheck:
    # Checks for gaps in hourly coverage for generation and market facts.
    lines = []
    any_large_gap = False

    sources = session.execute(select(DimEnergySource)).scalars().all()
    for source in sources:
        rows = session.execute(
            select(DimDatetime.timestamp_utc)
            .join(FactGeneration, FactGeneration.datetime_id == DimDatetime.datetime_id)
            .where(FactGeneration.energy_source_id == source.energy_source_id)
            .order_by(DimDatetime.timestamp_utc)
        ).scalars().all()
 
        if not rows:
            lines.append(f"- **{source.source_name}**: no data at all")
            any_large_gap = True
            continue
 
        expected_hours = int((rows[-1] - rows[0]).total_seconds() // 3600) + 1
        actual_hours = len(rows)
        missing = expected_hours - actual_hours
        pct_missing = 100 * missing / expected_hours if expected_hours else 0
 
        flag = " <- gap over 1%" if pct_missing > 1.0 else ""
        lines.append(
            f"- **{source.source_name}**: {rows[0].date()} to {rows[-1].date()}, "
            f"{missing:,} missing hours out of {expected_hours:,} ({pct_missing:.2f}%){flag}"
        )
        if pct_missing > 1.0:
            any_large_gap = True
 
    detail = "\n".join(lines)
    return ValidationCheck("Hourly coverage gaps (fact_generation, per source)", not any_large_gap, detail)


def check_nuclear_shutdown(session: Session) -> ValidationCheck:
    nuclear_source = session.execute(
        select(DimEnergySource).where(DimEnergySource.source_code == "nuclear")
    ).scalar_one_or_none()
    if nuclear_source is None:
        return ValidationCheck("Nuclear shutdown consistency", False, "No nuclear energy source row found at all.")
 
    post_shutdown_rows = session.execute(
        select(func.count())
        .select_from(FactGeneration)
        .join(DimDatetime, DimDatetime.datetime_id == FactGeneration.datetime_id)
        .where(
            FactGeneration.energy_source_id == nuclear_source.energy_source_id,
            DimDatetime.date > NUCLEAR_SHUTDOWN_DATE,
        )
    ).scalar()
 
    passed = post_shutdown_rows == 0
    detail = (
        f"Germany's last nuclear plants shut down {NUCLEAR_SHUTDOWN_DATE}. "
        f"Found {post_shutdown_rows} generation rows after that date "
        f"({'expected, matches known history' if passed else 'unexpected -- investigate source data'})."
    )
    return ValidationCheck("Nuclear shutdown consistency", passed, detail)


def check_negative_generation(session: Session) -> ValidationCheck:
    """Generation should never be negative -- unlike price, there's no real-world reason for it."""
    rows = session.execute(
        select(DimEnergySource.source_name, func.count())
        .select_from(FactGeneration)
        .join(DimEnergySource, DimEnergySource.energy_source_id == FactGeneration.energy_source_id)
        .where(FactGeneration.generation_mwh < 0)
        .group_by(DimEnergySource.source_name)
    ).all()
 
    if not rows:
        return ValidationCheck("Negative generation values", True, "None found across all energy sources.")
 
    detail = "\n".join(f"- {name}: {count:,} negative rows" for name, count in rows)
    return ValidationCheck("Negative generation values", False, detail)

def check_price_outliers(session: Session) -> ValidationCheck:
    extreme_low = session.execute(
        select(func.count()).select_from(FactMarket).where(FactMarket.price_eur_mwh < PRICE_REVIEW_LOW)
    ).scalar()
    extreme_high = session.execute(
        select(func.count()).select_from(FactMarket).where(FactMarket.price_eur_mwh > PRICE_REVIEW_HIGH)
    ).scalar()
    min_price, max_price = session.execute(
        select(func.min(FactMarket.price_eur_mwh), func.max(FactMarket.price_eur_mwh))
    ).one()
 
    detail = (
        f"Observed price range: {min_price:.2f} to {max_price:.2f} EUR/MWh.\n"
        f"- Below review threshold ({PRICE_REVIEW_LOW}): {extreme_low} rows\n"
        f"- Above review threshold ({PRICE_REVIEW_HIGH}): {extreme_high} rows\n\n"
        "Flagged, not failed -- extreme prices are real historical events "
        "(e.g. 2021-2022 energy crisis), not automatically data errors. "
        "Worth spot-checking flagged rows against known events before the written analysis."
    )
    # This check informs the analysis rather than gating it -- always "passes"
    # in the pass/fail sense, but the detail is what matters.
    return ValidationCheck("Price range / outlier scan", True, detail)

def check_negative_price_frequency(session: Session) -> ValidationCheck:
    total = session.execute(select(func.count()).select_from(FactMarket)).scalar()
    negative = session.execute(
        select(func.count()).select_from(FactMarket).where(FactMarket.price_eur_mwh < 0)
    ).scalar()
    pct = 100 * negative / total if total else 0
 
    yearly = session.execute(
        select(DimDatetime.year, func.count())
        .select_from(FactMarket)
        .join(DimDatetime, DimDatetime.datetime_id == FactMarket.datetime_id)
        .where(FactMarket.price_eur_mwh < 0)
        .group_by(DimDatetime.year)
        .order_by(DimDatetime.year)
    ).all()
    yearly_lines = "\n".join(f"  - {year}: {count} negative-price hours" for year, count in yearly)
    detail = (
        f"{negative:,} of {total:,} hours ({pct:.2f}%) had a negative day-ahead price overall.\n\n"
        f"By year:\n{yearly_lines}\n\n"
        "This is the core metric behind the business question -- whether this rises over time "
        "is exactly what the written analysis needs to check directly, not assume."
    )
    return ValidationCheck("Negative-price frequency (informational)", True, detail)

def check_renewable_share(session: Session) -> ValidationCheck:
    rows = session.execute(
        select(DimEnergySource.is_renewable, func.sum(FactGeneration.generation_mwh))
        .select_from(FactGeneration)
        .join(DimEnergySource, DimEnergySource.energy_source_id == FactGeneration.energy_source_id)
        .group_by(DimEnergySource.is_renewable)
    ).all()
 
    totals = {is_renewable: total for is_renewable, total in rows}
    renewable_total = totals.get(True, 0)
    non_renewable_total = totals.get(False, 0)
    grand_total = renewable_total + non_renewable_total
    pct = 100 * renewable_total / grand_total if grand_total else 0
 
    detail = (
        f"Renewable: {renewable_total:,.0f} MWh, Non-renewable: {non_renewable_total:,.0f} MWh, "
        f"Renewable share over full history: {pct:.1f}%\n\n"
        "Sanity check only -- confirms is_renewable flags are producing a plausible number "
        "(Germany's real renewable generation share is commonly cited in the 45-55% range "
        "in recent years), not a full analysis."
    )
    plausible = 20 < pct < 80  # wide guardrail, just catching a badly broken flag
    return ValidationCheck("Renewable share sanity check", plausible, detail)

def run_validation():
    engine = get_engine()
    checks = []
 
    with Session(engine) as session:
        checks.append(check_row_counts(session))
        checks.append(check_hourly_coverage_gaps(session))
        checks.append(check_nuclear_shutdown(session))
        checks.append(check_negative_generation(session))
        checks.append(check_price_outliers(session))
        checks.append(check_negative_price_frequency(session))
        checks.append(check_renewable_share(session))
 
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("# Data Validation Report\n\n")
        f.write("German Energy Market Analytics -- generated by `src/validate_data.py`\n\n")
        for check in checks:
            f.write(check.to_markdown())
            f.write("\n")
 
    n_passed = sum(1 for c in checks if c.passed)
    logger.info(f"Validation complete: {n_passed}/{len(checks)} checks passed. Report written to {REPORT_PATH}")
    for check in checks:
        status = "PASS" if check.passed else "FLAGGED"
        logger.info(f"  [{status}] {check.name}")
 
 
if __name__ == "__main__":
    run_validation()