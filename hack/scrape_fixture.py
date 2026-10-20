#!/usr/bin/env python3
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

"""Turn a component's /metrics into the fixture compose-serving-stack's scrape tests read.

    curl -s localhost:8000/metrics | hack/scrape_fixture.py "vllm/vllm-openai:v0.23.0" > \\
        functions/compose-serving-stack/tests/testdata/scrapes/vllm-v0.23.0.json

Scrape a pod that has served requests, so every labelled series exists. The
fixture records what the tests check: every name the component publishes, the
metric families from its # TYPE lines and the names of its samples, and each
histogram's bucket boundaries as published.
"""

import collections
import json
import re
import sys

SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s")
LE = re.compile(r'le="([^"]*)"')


def _bound(le: str) -> float:
    return float("inf") if le == "+Inf" else float(le)


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 1:
        sys.stderr.write("usage: scrape_fixture.py <what was scraped> < metrics > fixture.json\n")
        return 1
    names: set[str] = set()
    histograms: set[str] = set()
    buckets: dict[str, set[str]] = collections.defaultdict(set)
    for line in sys.stdin.read().splitlines():
        if line.startswith("# TYPE "):
            _, _, family, kind = line.split(maxsplit=3)
            names.add(family)
            if kind.strip() == "histogram":
                histograms.add(family)
        elif m := SAMPLE.match(line):
            names.add(m[1])
            if m[1].endswith("_bucket") and (le := LE.search(m[2] or "")):
                buckets[m[1].removesuffix("_bucket")].add(le[1])
    fixture = {
        "source": args[0],
        "names": sorted(names),
        "histograms": {h: sorted(buckets[h], key=_bound) for h in sorted(histograms)},
    }
    json.dump(fixture, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
