"""SMC feature module: market-structure & smart-money primitives.

Each submodule is a self-contained detector so you can extend or swap one
without touching the others.
"""
from . import structure, order_blocks, zones, liquidity, fvg, indicators, types  # noqa: F401
