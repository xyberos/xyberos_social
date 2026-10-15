"""Process metrics and a small Prometheus text exposition.

Every request is already logged structurally; these counters exist so an operator can
alert on rates, latency, and saturation without parsing logs. The exposition format is
implemented here rather than pulled in as a client library: it is a few dozen lines, and a
metrics dependency would be the only runtime dependency added for observability before
there is a scraper to feed.

Label values are bounded by construction. A route is labelled by its template, with the
matched path parameters substituted back out, and anything unmatched shares one
placeholder, so no path parameter can multiply a series.
"""

import time
from threading import Lock

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
UNMATCHED_ROUTE = "unmatched"
STATIC_ROUTE = "static"

_STARTED_AT = time.monotonic()


def uptime_seconds() -> float:
    return time.monotonic() - _STARTED_AT


def route_label(request) -> str:
    """The matched route template, with path parameter values substituted back out."""
    path = request.scope.get("path", "")
    if path.startswith("/static/"):
        return STATIC_ROUTE
    if "endpoint" not in request.scope:
        return UNMATCHED_ROUTE
    for name, value in (request.scope.get("path_params") or {}).items():
        if value:
            path = path.replace(str(value), f"{{{name}}}")
    return path


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def _format_value(value: float) -> str:
    return f"{value:g}"


def _format_labels(labels: dict[str, object]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{name}="{value}"' for name, value in sorted(labels.items()))
    return "{" + inner + "}"


class _Metric:
    type_name = "untyped"

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._values: dict[tuple, object] = {}
        self._lock = Lock()

    def _key(self, labels: dict[str, object]) -> tuple:
        # Every declared label is required: a missing one is a bug, not a silent zero.
        return tuple((name, str(labels[name])) for name in self.label_names)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def _header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.type_name}"]


class Counter(_Metric):
    type_name = "counter"

    def inc(self, amount: float = 1, **labels: object) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(self._values.get(key, 0.0)) + amount

    def value(self, **labels: object) -> float:
        with self._lock:
            return float(self._values.get(self._key(labels), 0.0))

    def render(self) -> list[str]:
        lines = self._header()
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            lines.append(f"{self.name}{_format_labels(dict(key))} {_format_value(value)}")
        return lines


class Histogram(_Metric):
    type_name = "histogram"

    def observe(self, seconds: float, **labels: object) -> None:
        key = self._key(labels)
        with self._lock:
            state = self._values.setdefault(key, [0] * len(LATENCY_BUCKETS) + [0.0, 0])
            for index, bound in enumerate(LATENCY_BUCKETS):
                if seconds <= bound:
                    state[index] += 1
            state[-2] += seconds
            state[-1] += 1

    def count(self, **labels: object) -> int:
        with self._lock:
            state = self._values.get(self._key(labels))
        return int(state[-1]) if state else 0

    def render(self) -> list[str]:
        lines = self._header()
        with self._lock:
            items = sorted(self._values.items())
        for key, state in items:
            labels = dict(key)
            for index, bound in enumerate(LATENCY_BUCKETS):
                bucket_labels = _format_labels({**labels, "le": bound})
                lines.append(f"{self.name}_bucket{bucket_labels} {_format_value(state[index])}")
            infinite = _format_labels({**labels, "le": "+Inf"})
            lines.append(f"{self.name}_bucket{infinite} {_format_value(state[-1])}")
            lines.append(f"{self.name}_sum{_format_labels(labels)} {_format_value(state[-2])}")
            lines.append(f"{self.name}_count{_format_labels(labels)} {_format_value(state[-1])}")
        return lines


class Registry:
    def __init__(self) -> None:
        self._metrics: list[_Metric] = []
        self._lock = Lock()

    def register(self, metric: _Metric) -> _Metric:
        with self._lock:
            self._metrics.append(metric)
        return metric

    def render(self, blocks: list[str] = ()) -> str:
        with self._lock:
            metrics = list(self._metrics)
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        for block in blocks:
            lines.extend(block.splitlines())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop every series. Only tests call this; production counters never reset."""
        with self._lock:
            metrics = list(self._metrics)
        for metric in metrics:
            metric.reset()


registry = Registry()

http_requests = registry.register(
    Counter(
        "soccom_http_requests_total",
        "HTTP requests by route template, method, and status class.",
        ("route", "method", "status"),
    )
)

http_request_duration = registry.register(
    Histogram(
        "soccom_http_request_duration_seconds",
        "HTTP request duration by route template and method.",
        ("route", "method"),
    )
)


def gauge(name: str, help_text: str, samples: list[tuple[dict[str, object], float]]) -> str:
    """Render a scrape-time gauge block, whose values are read rather than accumulated."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        lines.append(f"{name}{_format_labels(labels)} {_format_value(value)}")
    return "\n".join(lines)
