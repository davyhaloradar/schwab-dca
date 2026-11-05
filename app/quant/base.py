"""Quant / rule strategy plugin interface (stub for future strategies)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderIntent:
    symbol: str
    side: str = "BUY"  # BUY / SELL
    amount_usd: float | None = None
    quantity: int | None = None
    order_type: str = "MARKET"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyContext:
    """Runtime context passed into Strategy.evaluate."""
    plan: dict[str, Any]
    account_summary: dict[str, Any] | None = None
    quotes: dict[str, Any] | None = None
    dry_run: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """规则策略插件位 / Rule-strategy plugin slot."""

    name: str = "base"

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> list[OrderIntent]:
        """Return order intents for this evaluation cycle."""


class NoopStrategy(Strategy):
    """Default no-op: produces no additional intents (DCA engine drives buys)."""

    name = "noop"

    def evaluate(self, ctx: StrategyContext) -> list[OrderIntent]:
        return []
