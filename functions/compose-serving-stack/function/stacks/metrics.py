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

"""What Modelplane calls each metric the components it installs emit.

Reviewed, pinned data, the same discipline as the component lists. A
MetricMapping is the extension point for a component Modelplane ships no
statements for; these are the ones it does.

Renaming is only safe where the measurements agree. SGLang's
inter_token_latency is not vLLM's time per output token, so neither is
renamed onto a shared name and the gateway supplies that measurement for
both. A histogram is renamed only where its bucket boundaries match, which is
why SGLang's latency histograms are absent here: they resolve to a hundred
milliseconds where vLLM's resolve to one, and a quantile across the two is
wrong rather than approximate.

Matching has to hold across the fleet, not just between two engines, because
a query sums buckets by boundary and a series with different boundaries
corrupts the sum for everyone. So each modelplane_* histogram has
exactly one source. The engines' token histograms can't meet that even alone:
vLLM sizes their buckets from --max-model-len, so two vLLM deployments
disagree. They leave as counters instead, a sum and a count having no buckets.

docs/utils/validate/validate_manifests.py holds every recipe's engine to the
versions and flags these mappings need, so a change to one changes both.
"""

from models.ai.modelplane.metricmapping import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The front door, which measures every request it proxies under the
# OpenTelemetry GenAI conventions, for whatever engine is behind it. These are
# the SLO metrics: one component, one bucket layout, so a fleet quantile over
# them is sound.
_GATEWAY = {
    "gen_ai_server_request_duration_seconds": "modelplane_frontend_request_duration_seconds",
    "gen_ai_server_time_to_first_token_seconds": "modelplane_frontend_ttft_seconds",
    "gen_ai_server_time_per_output_token_seconds": "modelplane_frontend_tpot_seconds",
}

# The engines, which explain what the gateway measured.
_VLLM = {
    "vllm:time_to_first_token_seconds": "modelplane_request_ttft_seconds",
    "vllm:e2e_request_latency_seconds": "modelplane_request_duration_seconds",
    "vllm:request_queue_time_seconds": "modelplane_request_queue_seconds",
    "vllm:request_prefill_time_seconds": "modelplane_request_prefill_seconds",
    "vllm:request_decode_time_seconds": "modelplane_request_decode_seconds",
    "vllm:num_requests_running": "modelplane_requests_running",
    "vllm:num_requests_waiting": "modelplane_requests_waiting",
    "vllm:kv_cache_usage_perc": "modelplane_kv_cache_utilization_ratio",
    "vllm:num_preemptions_total": "modelplane_requests_preempted_total",
    "vllm:prefix_cache_hits_total": "modelplane_prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total": "modelplane_prefix_cache_lookups_total",
    # The rest are published only when the engine is started with the feature:
    # a prefix cache outside the GPU, such as the host-RAM KV offload tier
    # (from v0.23.0), that tier's transfers (--kv-offloading-backend, from
    # v0.27.1), and speculative decoding. kv_offload_cpu_cache_usage_perc is the
    # fraction of the tier's chunks in transfer, neither free nor evictable, not
    # how full the tier is, and nothing publishes how full it is.
    "vllm:external_prefix_cache_hits_total": "modelplane_external_prefix_cache_hits_total",
    "vllm:external_prefix_cache_queries_total": "modelplane_external_prefix_cache_lookups_total",
    "vllm:kv_offload_store_bytes_total": "modelplane_kv_offload_store_bytes_total",
    "vllm:kv_offload_load_bytes_total": "modelplane_kv_offload_load_bytes_total",
    "vllm:kv_offload_store_time_total": "modelplane_kv_offload_store_seconds_total",
    "vllm:kv_offload_load_time_total": "modelplane_kv_offload_load_seconds_total",
    "vllm:kv_offload_cpu_cache_usage_perc": "modelplane_kv_offload_in_transfer_ratio",
    "vllm:spec_decode_num_drafts_total": "modelplane_spec_decode_drafts_total",
    "vllm:spec_decode_num_draft_tokens_total": "modelplane_spec_decode_draft_tokens_total",
    # By draft position, in the engine's own position label.
    "vllm:spec_decode_num_accepted_tokens_per_pos_total": "modelplane_spec_decode_accepted_tokens_total",
}

# SGLang's prefix-cache and retraction counters count what vLLM's do, in the
# same units: prefix-cache hits and lookups in prompt tokens, so hits over
# lookups is a hit rate for either engine. SGLang counts when a request
# finishes where vLLM counts when it's scheduled, and labels its hits with a
# cache_source (device, host, storage). sglang:queue_time_seconds is a
# per-request histogram like vLLM's queue time, but its buckets differ from
# vLLM's and between SGLang versions, so it isn't renamed (#523).
_SGLANG = {
    "sglang:num_running_reqs": "modelplane_requests_running",
    "sglang:num_queue_reqs": "modelplane_requests_waiting",
    "sglang:token_usage": "modelplane_kv_cache_utilization_ratio",
    "sglang:num_retracted_requests_total": "modelplane_requests_preempted_total",
    "sglang:cached_tokens_total": "modelplane_prefix_cache_hits_total",
    "sglang:prompt_tokens_total": "modelplane_prefix_cache_lookups_total",
}

# Each engine's prompt and generation token histograms, taken as counters: the
# prompt and generated tokens of the requests it finished, and from the prompt
# histogram's count, the requests. A prompt's tokens include any the prefix
# cache served. The histograms themselves are dropped (see the module docstring).
# SGLang v0.5.10 publishes these histograms only with
# --collect-tokens-histogram as well as --enable-metrics; from v0.5.12 it
# always does.
_VLLM_TOKENS = ("vllm:request_prompt_tokens", "vllm:request_generation_tokens")
_SGLANG_TOKENS = ("sglang:prompt_tokens_histogram", "sglang:generation_tokens_histogram")

# The endpoint picker. A router's queue is a different measurement from an
# engine's, so it keeps a name of its own.
_PICKER = {
    "llm_d_epp_scheduler_e2e_duration_seconds": "modelplane_route_decision_seconds",
}

# The GPUs, through whichever vendor's exporter the stack installed. DCGM
# reports framebuffer memory in MiB and energy in millijoules, and each is
# renamed onto a name that claims a different unit, so each says so. PCIe
# traffic is a rate DCGM samples, in bytes per second, though some exporters
# type it a counter (see collector._SAMPLED_RATES). GKE's managed exporter
# doesn't publish SM occupancy.
_GPU = {
    "DCGM_FI_DEV_FB_USED": "modelplane_gpu_memory_used_bytes",
    "DCGM_FI_DEV_FB_FREE": "modelplane_gpu_memory_free_bytes",
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE": "modelplane_gpu_compute_active_ratio",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "modelplane_gpu_tensor_active_ratio",
    "DCGM_FI_PROF_DRAM_ACTIVE": "modelplane_gpu_memory_bandwidth_ratio",
    "DCGM_FI_DEV_GPU_TEMP": "modelplane_gpu_temperature_celsius",
    "DCGM_FI_DEV_POWER_USAGE": "modelplane_gpu_power_watts",
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "modelplane_energy_joules_total",
    "DCGM_FI_PROF_PCIE_TX_BYTES": "modelplane_gpu_pcie_transmit_bytes_per_second",
    "DCGM_FI_PROF_PCIE_RX_BYTES": "modelplane_gpu_pcie_receive_bytes_per_second",
    "DCGM_FI_PROF_SM_ACTIVE": "modelplane_gpu_sm_active_ratio",
    "DCGM_FI_PROF_SM_OCCUPANCY": "modelplane_gpu_sm_occupancy_ratio",
}

_GPU_UNITS = {
    "DCGM_FI_DEV_FB_USED": "Mebibytes",
    "DCGM_FI_DEV_FB_FREE": "Mebibytes",
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "Millijoules",
}


def _tokens(prompt: str, generation: str) -> list[dict[str, object]]:
    """An engine's token histograms as tokens by direction, and requests."""
    return [
        {
            "from": prompt,
            "part": "Sum",
            "to": "modelplane_engine_tokens_total",
            "labels": [{"name": "direction", "value": "input"}],
        },
        {
            "from": generation,
            "part": "Sum",
            "to": "modelplane_engine_tokens_total",
            "labels": [{"name": "direction", "value": "output"}],
        },
        {"from": prompt, "part": "Count", "to": "modelplane_engine_requests_total"},
    ]


def _mapping(
    name: str,
    pairs: dict[str, str],
    units: dict[str, str] | None = None,
    tokens: tuple[str, str] | None = None,
) -> v1alpha1.MetricMapping:
    units = units or {}
    metrics: list[dict[str, object]] = [
        {"from": src, "to": dst} | ({"fromUnit": units[src]} if src in units else {}) for src, dst in pairs.items()
    ]
    if tokens:
        metrics += _tokens(*tokens)
    return v1alpha1.MetricMapping(
        metadata=metav1.ObjectMeta(name=name),
        spec=v1alpha1.Spec(metrics=[v1alpha1.Metric.model_validate(m) for m in metrics]),
    )


def mappings() -> list[v1alpha1.MetricMapping]:
    """The mappings Modelplane provides, as the kind an operator would write.

    Built as MetricMappings rather than as collector configuration so that
    Modelplane's own renames reach the collector down the same path an
    operator's do, and a built-in that breaks breaks the path everyone uses.
    """
    return [
        _mapping("modelplane-gateway", _GATEWAY),
        _mapping("modelplane-vllm", _VLLM, tokens=_VLLM_TOKENS),
        _mapping("modelplane-sglang", _SGLANG, tokens=_SGLANG_TOKENS),
        _mapping("modelplane-picker", _PICKER),
        _mapping("modelplane-gpu", _GPU, _GPU_UNITS),
    ]


BUILTIN_MAPPINGS = mappings()
