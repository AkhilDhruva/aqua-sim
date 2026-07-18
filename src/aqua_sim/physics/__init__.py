"""Physics substrate: the shallow-water solver and its numerics."""

from aqua_sim.physics.boundary import BoundaryType
from aqua_sim.physics.friction import manning_velocity
from aqua_sim.physics.stability import cfl_timestep
from aqua_sim.physics.swe import FlowState, ShallowWaterSolver

__all__ = [
    "BoundaryType",
    "manning_velocity",
    "cfl_timestep",
    "FlowState",
    "ShallowWaterSolver",
]
