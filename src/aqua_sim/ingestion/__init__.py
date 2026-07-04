"""Terrain ingestion: every input format converges to a single ``Grid``."""

from aqua_sim.ingestion.base import TerrainSource
from aqua_sim.ingestion.synthetic import SyntheticTerrain

__all__ = ["TerrainSource", "SyntheticTerrain"]
