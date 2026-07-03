from __future__ import annotations

import pytest

from agentflow.inference.skypilot import build_sky_resources_kwargs, parse_gpu_selector


def test_parse_single_node_cloud_region_gpu_selector():
    selector = parse_gpu_selector("aws:8xb200@us-east-1")

    assert selector.cloud == "aws"
    assert selector.location == "us-east-1"
    assert selector.infra == "aws/us-east-1"
    assert selector.accelerator == "B200"
    assert selector.count == 8
    assert selector.num_nodes is None
    assert selector.accelerators == "B200:8"


def test_parse_multi_node_multi_gpu_selector():
    selector = parse_gpu_selector("aws:8x8xb200@us-east-2")

    assert selector.cloud == "aws"
    assert selector.location == "us-east-2"
    assert selector.infra == "aws/us-east-2"
    assert selector.accelerator == "B200"
    assert selector.count == 8
    assert selector.num_nodes == 8
    assert selector.accelerators == "B200:8"


def test_parse_provider_agnostic_selector():
    selector = parse_gpu_selector("1xl4")

    assert selector.cloud is None
    assert selector.infra is None
    assert selector.accelerator == "L4"
    assert selector.count == 1


def test_build_sky_resources_kwargs_defaults_to_spot():
    selector = parse_gpu_selector("aws:1xl4@us-east-1")

    assert build_sky_resources_kwargs(selector, use_spot=True, max_hourly_cost=2.5) == {
        "infra": "aws/us-east-1",
        "accelerators": "L4:1",
        "use_spot": True,
        "max_hourly_cost": 2.5,
    }


@pytest.mark.parametrize("value", ["", "aws:", "aws:0xb200", "aws:8x0xb200", "aws:8x"])
def test_parse_gpu_selector_rejects_invalid_values(value: str):
    with pytest.raises(ValueError):
        parse_gpu_selector(value)
