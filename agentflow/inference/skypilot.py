from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_GPU_SELECTOR_RE = re.compile(
    r"^(?:(?P<cloud>[^:@\s]+):)?(?P<shape>[^@\s]+)(?:@(?P<location>[^@\s]+))?$"
)
_MULTI_NODE_GPU_RE = re.compile(r"^(?P<nodes>\d+)x(?P<count>\d+)x(?P<name>[A-Za-z0-9_.-]+)$")
_GPU_COUNT_RE = re.compile(r"^(?P<count>\d+)x(?P<name>[A-Za-z0-9_.-]+)$")
_SKY_ACCEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+):(?P<count>\d+)$")
_GPU_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class GpuSelector:
    """Normalized AgentFlow shorthand for SkyPilot resources."""

    raw: str
    accelerator: str
    count: int = 1
    cloud: str | None = None
    location: str | None = None
    num_nodes: int | None = None

    @property
    def infra(self) -> str | None:
        if self.cloud is None:
            return None
        if self.location is None:
            return self.cloud
        return f"{self.cloud}/{self.location}"

    @property
    def accelerators(self) -> str:
        return f"{self.accelerator}:{self.count}"


@dataclass(frozen=True, slots=True)
class SkyInferenceRequest:
    model_id: str
    gpu: GpuSelector
    engine: str = "vllm"
    input_path: Path | None = None
    output_path: Path | None = None
    use_spot: bool = True
    max_hourly_cost: float | None = None
    name: str | None = None
    workers: int | None = None
    detach: bool = False
    pool: str | None = None


def parse_gpu_selector(value: str) -> GpuSelector:
    """Parse `aws:8xb200@us-east-1` and `aws:8x8xb200@us-east-2` style selectors."""

    raw = value.strip()
    if not raw:
        raise ValueError("GPU selector must not be empty")

    match = _GPU_SELECTOR_RE.fullmatch(raw)
    if match is None:
        raise ValueError(
            "GPU selector must look like `aws:8xb200@us-east-1`, `aws:8x8xb200@us-east-2`, or `8xh100`"
        )

    cloud = match.group("cloud")
    location = match.group("location")
    shape = match.group("shape")
    num_nodes: int | None = None
    count = 1
    accelerator: str

    multi_node_match = _MULTI_NODE_GPU_RE.fullmatch(shape)
    if multi_node_match is not None:
        num_nodes = _positive_int(multi_node_match.group("nodes"), "node count")
        count = _positive_int(multi_node_match.group("count"), "GPU count")
        accelerator = multi_node_match.group("name")
    else:
        count_match = _GPU_COUNT_RE.fullmatch(shape)
        sky_match = _SKY_ACCEL_RE.fullmatch(shape)
        if count_match is not None:
            count = _positive_int(count_match.group("count"), "GPU count")
            accelerator = count_match.group("name")
        elif sky_match is not None:
            count = _positive_int(sky_match.group("count"), "GPU count")
            accelerator = sky_match.group("name")
        elif "x" not in shape.lower() and _GPU_NAME_RE.fullmatch(shape):
            accelerator = shape
        else:
            raise ValueError(f"Invalid GPU selector shape `{shape}`")

    return GpuSelector(
        raw=raw,
        cloud=_normalize_cloud(cloud),
        location=location,
        accelerator=_normalize_accelerator(accelerator),
        count=count,
        num_nodes=num_nodes,
    )


def build_sky_resources_kwargs(selector: GpuSelector, *, use_spot: bool, max_hourly_cost: float | None) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "accelerators": selector.accelerators,
        "use_spot": use_spot,
    }
    if selector.infra is not None:
        kwargs["infra"] = selector.infra
    if max_hourly_cost is not None:
        kwargs["max_hourly_cost"] = max_hourly_cost
    return kwargs


def _positive_int(value: str, label: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _normalize_cloud(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _normalize_accelerator(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("accelerator name must not be empty")
    if normalized.lower().startswith("tpu-"):
        return normalized
    return normalized.upper()
