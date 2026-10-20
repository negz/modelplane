# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the built-in mappings against what the components really publish.

The mappings are written against upstream source, and nothing else checks them
against a running component: a name an engine version doesn't publish leaves a
modelplane_* series empty with no error, and two histograms with different
buckets under one name corrupt every quantile over it. Each file under
testdata/scrapes/ records what one component version publishes, taken by
hack/scrape_fixture.py from the /metrics of a running pod that had served
requests, so that every labelled series exists: every name, and each
histogram's bucket boundaries. When bumping a version, scrape it into a file of
its own and add a case for it.
"""

import collections
import dataclasses
import json
import pathlib

import pytest
from function import stacks
from models.ai.modelplane.metricmapping import v1alpha1 as mmv1alpha1


@dataclasses.dataclass
class Scrape:
    """What one component version publishes, as the collector would scrape it."""

    names: set[str]
    # Each histogram family's bucket boundaries, as published.
    buckets: dict[str, frozenset[str]]


def _scrape(*, file: str) -> Scrape:
    """The scrape in testdata/scrapes/file."""
    path = pathlib.Path(__file__).parent / "testdata" / "scrapes" / file
    fixture = json.loads(path.read_text())
    return Scrape(
        names=set(fixture["names"]),
        buckets={h: frozenset(bounds) for h, bounds in fixture["histograms"].items()},
    )


def _sources(*, mapping: str) -> list[mmv1alpha1.Metric]:
    """The metrics the built-in mapping named mapping reads."""
    return next(m.spec.metrics for m in stacks.BUILTIN_MAPPINGS if m.metadata and m.metadata.name == mapping)


@dataclasses.dataclass
class PublishedCase:
    """A test case for the names a component version publishes."""

    name: str
    reason: str
    mapping: str
    file: str
    # Names the component publishes only from the first event it counts, so a
    # scrape of a lightly loaded pod can't show them, checked against the
    # component's source at that version instead.
    unscraped: tuple[str, ...]
    want: list[str]


PUBLISHED_CASES = [
    PublishedCase(
        name="Gateway",
        reason="Envoy AI Gateway v1.1.0 publishes every name the gateway mapping reads.",
        mapping="modelplane-gateway",
        file="gateway-v1.1.0.json",
        unscraped=(),
        want=[],
    ),
    PublishedCase(
        name="VLLMOlder",
        reason="vLLM v0.11.0 publishes every name the vLLM mapping reads.",
        mapping="modelplane-vllm",
        file="vllm-v0.11.0.json",
        unscraped=(),
        want=[],
    ),
    PublishedCase(
        name="VLLMNewer",
        reason="vLLM v0.23.0 publishes every name the vLLM mapping reads.",
        mapping="modelplane-vllm",
        file="vllm-v0.23.0.json",
        unscraped=(),
        want=[],
    ),
    # SGLang runs prometheus_client in multiprocess mode, which writes a
    # labelled counter only once it has a value. Its retraction counter is
    # incremented when the KV cache runs out, in metrics_collector.py at both
    # versions.
    PublishedCase(
        name="SGLangOlder",
        reason="SGLang v0.5.10 publishes every name the SGLang mapping reads.",
        mapping="modelplane-sglang",
        file="sglang-v0.5.10.post1.json",
        unscraped=("sglang:num_retracted_requests_total",),
        want=[],
    ),
    PublishedCase(
        name="SGLangNewer",
        reason="SGLang v0.5.12 publishes every name the SGLang mapping reads.",
        mapping="modelplane-sglang",
        file="sglang-v0.5.12.post1.json",
        unscraped=("sglang:num_retracted_requests_total",),
        want=[],
    ),
    PublishedCase(
        name="Picker",
        reason="The llm-d endpoint picker v0.9.0 publishes every name the picker mapping reads.",
        mapping="modelplane-picker",
        file="picker-v0.9.0.json",
        unscraped=(),
        want=[],
    ),
    PublishedCase(
        name="GKEDCGM",
        reason="GKE's managed DCGM exporter publishes every name the GPU mapping reads.",
        mapping="modelplane-gpu",
        file="dcgm-gke.json",
        unscraped=(),
        want=[],
    ),
]


@pytest.mark.parametrize("case", PUBLISHED_CASES, ids=lambda case: case.name)
def test_published(case: PublishedCase) -> None:
    """Every name a built-in mapping reads is one its component version publishes."""
    names = _scrape(file=case.file).names
    got = sorted({m.from_ for m in _sources(mapping=case.mapping)} - names - set(case.unscraped))
    assert got == case.want, case.reason


def test_mappings_scraped() -> None:
    """Each built-in mapping has a scrape of the component it reads."""
    scraped = {case.mapping for case in PUBLISHED_CASES}
    builtin = {m.metadata.name for m in stacks.BUILTIN_MAPPINGS if m.metadata}
    assert scraped == builtin, "a built-in mapping has no scrape to check it against"


def test_histogram_layouts() -> None:
    """Each modelplane_* histogram has one source, with the same buckets in every version scraped."""
    # A query sums a histogram's buckets by boundary, so one series with other
    # boundaries corrupts the quantile for the whole fleet. A part (a sum or a
    # count) is a counter and has no buckets, so it's free to share a name.
    sources: dict[str, set[str]] = collections.defaultdict(set)
    layouts: dict[str, set[frozenset[str]]] = collections.defaultdict(set)
    for case in PUBLISHED_CASES:
        buckets = _scrape(file=case.file).buckets
        for m in _sources(mapping=case.mapping):
            if not m.part and m.from_ in buckets:
                sources[m.to].add(m.from_)
                layouts[m.to].add(buckets[m.from_])
    assert {to: sorted(s) for to, s in sources.items() if len(s) > 1} == {}, "a histogram takes more than one source"
    assert sorted(to for to, ls in layouts.items() if len(ls) > 1) == [], "a histogram's buckets differ by version"
