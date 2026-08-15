"""
SQLAlchemy models for the German Energy Market Analytics.

Fact Tables:
- FactGeneration : grain = (datetime,region,energy_source)
- FactMarket :     grain = (datetime,region)

Dimension Tables:
-DimDateTime
-DimRegion
-DimEnergySource
"""

from datetime import datetime
from pathlib import Path
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Float,
    String,
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship,Session
Base = declarative_base()

#Dimension Tables

class DimDatetime(Base):
    __tablename__ = "dim_datetime"

    datetime_id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc = Column(DateTime, nullable=False, unique = True) # the actual hour.
    date = Column(Date,nullable=False)
    hour = Column(Integer,nullable=False)
    day_of_week = Column(Integer,nullable=False)
    is_weekend = Column(Boolean,nullable=False)
    month = Column(Integer,nullable = False)
    quarter = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)

    generation_facts = relationship("FactGeneration", back_populates="datetime_dim")
    market_facts = relationship("FactMarket", back_populates="datetime_dim")



class DimRegion(Base):
    __tablename__ = "dim_region"

    region_id = Column(Integer, primary_key=True, autoincrement=True)
    region_code = Column(String(20), nullable=False,unique=True) # "DE", "TENNET", "50HERTZ", "AMPRION", "TRANSNETBW"
    region_name = Column(String(100), nullable=False)

    generation_facts = relationship("FactGeneration", back_populates="region_dim")
    market_facts = relationship("FactMarket", back_populates="region_dim")

class DimEnergySource(Base):
    __tablename__ = "dim_energy_source"

    energy_source_id = Column(Integer, primary_key=True, autoincrement=True)
    source_code = Column(String(50), nullable=False,unique=True)
    source_name = Column(String(100), nullable=False)
    is_renewable = Column(Boolean, nullable=False)

    generation_facts = relationship("FactGeneration", back_populates="energy_source_dim")


# FACT TABLES

class FactGeneration(Base):
    __tablename__ = "fact_generation"

    fact_generation_id = Column(Integer, primary_key=True, autoincrement=True)

    datetime_id = Column(Integer, ForeignKey("dim_datetime.datetime_id"),nullable=False)
    region_id = Column(Integer, ForeignKey("dim_region.region_id"), nullable=False)
    energy_source_id = Column(Integer, ForeignKey("dim_energy_source.energy_source_id"), nullable=False)

    generation_mwh = Column(Float, nullable=False)

    datetime_dim = relationship("DimDatetime", back_populates="generation_facts")
    region_dim = relationship("DimRegion", back_populates="generation_facts")
    energy_source_dim = relationship("DimEnergySource", back_populates="generation_facts")

    __table_args__ = (
        #this is first line of defense against duplicate API's pulls
        UniqueConstraint("datetime_id", "region_id", "energy_source_id", name="uq_generation_grain"),
    )  


class FactMarket(Base):
    __tablename__ = "fact_market"

    fact_market_id = Column(Integer, primary_key=True, autoincrement=True)

    datetime_id = Column(Integer, ForeignKey("dim_datetime.datetime_id"), nullable=False)
    region_id = Column(Integer, ForeignKey("dim_region.region_id"), nullable=False)

    price_eur_mwh = Column(Float, nullable=False)   # can be negative -- that's a real, valid value, not an error
    load_mwh = Column(Float, nullable=True)          # nullable: not every region reports load the same way

    datetime_dim = relationship("DimDatetime", back_populates="market_facts")
    region_dim = relationship("DimRegion", back_populates="market_facts")

    __table_args__ = (
        UniqueConstraint("datetime_id", "region_id", name="uq_market_grain"),
    )

#SETUP
def get_engine(db_path: str | None = None):
    """SQLite for local dev -- swap the connection string later if you want Postgres."""
    if db_path is None:
        db_path = str(Path(__file__).resolve().parent.parent / "energy_market.db")
    return create_engine(f"sqlite:///{db_path}", echo=False)


def init_db(engine):
    """Creates all tables if they don't already exist."""
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    engine = get_engine()
    init_db(engine)
    print("Schema created: dim_datetime, dim_region, dim_energy_source, fact_generation, fact_market")