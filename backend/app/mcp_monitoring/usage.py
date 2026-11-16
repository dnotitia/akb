"""Server-only tool metric declarations. MCP tool schemas stay wire-compatible."""

import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from mcp.types import Tool
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    read: Callable[[dict, dict], int | bool | None]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ZeroRate:
    key: str
    label: str
    result_field: str

    def sample(self, args, result):
        value = result.get(self.result_field)
        if type(value) is int and value >= 0:
            return value == 0
        return None

    def descriptor(self):
        return {"kind": "zero_rate", "label": self.label, "dimensions": []}


@dataclass(frozen=True)
class Latency:
    key: str
    label: str
    dimensions: tuple[Dimension, ...]

    def sample(self, args, result):
        values = {}
        for dimension in self.dimensions:
            value = dimension.read(args, result)
            if value is not None and type(value) is not bool and not (type(value) is int and -(2**31) <= value < 2**31):
                return None
            values[dimension.key] = value
        return values

    def descriptor(self):
        return {"kind": "latency", "label": self.label, "dimensions": [
            {"key": d.key, "label": d.label, "labels": d.labels} for d in self.dimensions
        ]}


type Metric = ZeroRate | Latency


class MeasuredTool(Tool):
    _metrics: tuple[Metric, ...] = PrivateAttr(default=())

    @property
    def metrics(self):
        return self._metrics


def measured_tool(*metrics: Metric, **definition) -> MeasuredTool:
    tool = MeasuredTool(**definition)
    tool._metrics = metrics
    if len({m.key for m in metrics}) != len(metrics):
        raise ValueError(f"Duplicate metric on {tool.name}")
    return tool


@dataclass
class CallMeasurements:
    metrics: tuple[Metric, ...]
    values: dict = field(default_factory=dict)


current_usage: ContextVar[CallMeasurements | None] = ContextVar("mcp_tool_usage", default=None)


def measure_result(arguments: dict, result: dict) -> None:
    """Read the business result before wire encoding, only inside an observed call."""
    usage = current_usage.get()
    if usage is None:
        return
    for metric in usage.metrics:
        try:
            sample = metric.sample(arguments, result)
            if sample is not None:
                usage.values[metric.key] = sample
        except Exception:
            # A broken declaration must not affect the tool or its other metrics.
            logger.warning("Could not measure declared MCP metric %s", metric.key)
