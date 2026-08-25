"""Strategy plugins.

Add one:
  1. new file here, subclass Strategy, decorate with @strategy_registry.register("name")
  2. add a params YAML under config/strategies/
  3. add a block to `strategies:` in config/default.yaml

Nothing else changes.
"""

from oaa.strategies.base import Strategy, StrategyContext, load_strategies, strategy_registry

__all__ = ["Strategy", "StrategyContext", "load_strategies", "strategy_registry"]
