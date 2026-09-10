"""Optimized launcher for the LoRaWAN gateway estimator.

Run with:
    streamlit run streamlit_fast.py

The launcher applies result-preserving hot-path optimizations before loading the
existing Streamlit application. The original main branch and gateway_estimation.py
remain untouched so results can be compared side by side.
"""

from __future__ import annotations

import math
import runpy
from time import perf_counter

import coverage_model as cm


# Streamlit executes this launcher again on every rerun while keeping imported
# modules alive. Persist original callables on coverage_model so wrappers never
# wrap wrappers and timing output remains one line per actual model execution.
if not hasattr(cm, "_perf_original_plan_coverage"):
    cm._perf_original_plan_coverage = cm.plan_coverage
if not hasattr(cm, "_perf_original_coverage_sets"):
    cm._perf_original_coverage_sets = cm._coverage_sets
if not hasattr(cm, "_perf_original_greedy_multicover"):
    cm._perf_original_greedy_multicover = cm._greedy_multicover
if not hasattr(cm, "_perf_original_derive_sf_distribution"):
    cm._perf_original_derive_sf_distribution = cm._derive_sf_distribution


def fast_link_margins_db(
    gateway,
    device,
    azimuth_deg,
    radio,
    antenna,
    obstacles=None,
):
    """Equivalent link-margin calculation with shared geometry/path-loss work."""
    distance_m = max(math.dist(gateway, device), 1.0)
    antenna_loss = cm.antenna_attenuation_db(
        gateway, device, azimuth_deg, antenna
    )
    obstacle_loss = cm.obstacle_attenuation_db(
        gateway, device, radio, obstacles
    )
    path_loss = (
        cm.free_space_path_loss_1m_db(radio.frequency_mhz)
        + 10 * radio.path_loss_exponent * math.log10(distance_m)
        + radio.additional_loss_db
    )

    uplink_received = (
        radio.tx_eirp_dbm
        + radio.device_antenna_gain_dbi
        + radio.gateway_gain_dbi
        - radio.gateway_cable_loss_db
        - radio.device_installation_loss_db
        - path_loss
        - antenna_loss
        - obstacle_loss
    )
    downlink_received = (
        radio.gateway_tx_eirp_dbm
        + radio.device_antenna_gain_dbi
        - radio.device_installation_loss_db
        - path_loss
        - antenna_loss
        - obstacle_loss
    )
    uplink_margin = uplink_received - radio.effective_receiver_sensitivity_dbm
    downlink_margin = downlink_received - radio.device_receiver_sensitivity_dbm
    limiting_margin = (
        min(uplink_margin, downlink_margin)
        if radio.validate_downlink
        else uplink_margin
    )
    return uplink_margin, downlink_margin, limiting_margin


def fast_spread_sample_points(points, max_count):
    """Exact farthest-point strategy without recomputing all prior distances."""
    unique_points = list(dict.fromkeys(points))
    if len(unique_points) <= max_count:
        return unique_points

    center = (
        sum(point[0] for point in unique_points) / len(unique_points),
        sum(point[1] for point in unique_points) / len(unique_points),
    )
    first = min(
        range(len(unique_points)),
        key=lambda index: math.dist(unique_points[index], center),
    )
    selected = [first]
    available = set(range(len(unique_points))) - {first}
    minimum_distances = {
        index: math.dist(unique_points[index], unique_points[first])
        for index in available
    }

    while available and len(selected) < max_count:
        best = max(available, key=lambda index: minimum_distances[index])
        selected.append(best)
        available.remove(best)
        best_point = unique_points[best]
        for index in available:
            distance = math.dist(unique_points[index], best_point)
            if distance < minimum_distances[index]:
                minimum_distances[index] = distance

    return [unique_points[index] for index in selected]


def timed_coverage_sets(*args, **kwargs):
    start = perf_counter()
    result = cm._perf_original_coverage_sets(*args, **kwargs)
    elapsed = perf_counter() - start
    candidates = len(args[0]) if args else 0
    evaluation_points = len(args[2]) if len(args) > 2 else 0
    print(
        "[coverage-stage] "
        f"coverage_sets={elapsed:.3f}s "
        f"candidates={candidates} evaluation_points={evaluation_points} "
        f"pairs={candidates * evaluation_points}",
        flush=True,
    )
    return result


def timed_greedy_multicover(*args, **kwargs):
    start = perf_counter()
    result = cm._perf_original_greedy_multicover(*args, **kwargs)
    elapsed = perf_counter() - start
    candidate_count = len(args[0]) if args else 0
    point_count = args[5] if len(args) > 5 else 0
    print(
        "[coverage-stage] "
        f"greedy_multicover={elapsed:.3f}s "
        f"candidates={candidate_count} points={point_count}",
        flush=True,
    )
    return result


def timed_derive_sf_distribution(*args, **kwargs):
    start = perf_counter()
    result = cm._perf_original_derive_sf_distribution(*args, **kwargs)
    elapsed = perf_counter() - start
    evaluation_points = len(args[0]) if args else 0
    selected_gateways = len(args[1]) if len(args) > 1 else 0
    print(
        "[coverage-stage] "
        f"sf_distribution={elapsed:.3f}s "
        f"evaluation_points={evaluation_points} selected_gateways={selected_gateways}",
        flush=True,
    )
    return result


def timed_plan_coverage(*args, **kwargs):
    """Keep server-side timing visible without changing CoveragePlan serialization."""
    start = perf_counter()
    result = cm._perf_original_plan_coverage(*args, **kwargs)
    elapsed = perf_counter() - start
    links = len(result.candidate_points) * len(result.evaluation_points)
    print(
        "[coverage-performance] "
        f"total={elapsed:.3f}s "
        f"evaluation_points={len(result.evaluation_points)} "
        f"candidates={len(result.candidate_points)} "
        f"candidate_point_pairs={links} "
        f"selected_gateways={len(result.selected_points)}",
        flush=True,
    )
    return result


# Patch internal hot paths. Assigning on every Streamlit rerun is safe because the
# originals above are persisted once and the wrappers always call those originals.
cm.link_margins_db = fast_link_margins_db
cm._spread_sample_points = fast_spread_sample_points
cm._coverage_sets = timed_coverage_sets
cm._greedy_multicover = timed_greedy_multicover
cm._derive_sf_distribution = timed_derive_sf_distribution
cm.plan_coverage = timed_plan_coverage

# gateway_estimation imports plan_coverage after the patches have been installed,
# so its existing Streamlit cache continues to work normally.
runpy.run_module("gateway_estimation", run_name="__main__")
