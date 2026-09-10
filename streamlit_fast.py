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


_ORIGINAL_PLAN_COVERAGE = cm.plan_coverage


def fast_link_margins_db(
    gateway,
    device,
    azimuth_deg,
    radio,
    antenna,
    obstacles=None,
):
    """Equivalent link-margin calculation with shared geometry/path-loss work.

    The original implementation calculates uplink and downlink through two separate
    functions. Both repeat distance, antenna attenuation, obstacle intersection and
    path-loss calculations. This version calculates those common terms once.
    """
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
    """Exact farthest-point strategy without recomputing all prior distances.

    The original routine recomputes distance from every available point to every
    already-selected point on every iteration. Maintaining each point's current
    minimum distance reduces the inner work substantially while preserving the same
    farthest-point criterion.
    """
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


def timed_plan_coverage(*args, **kwargs):
    """Keep server-side timing visible without changing CoveragePlan serialization."""
    start = perf_counter()
    result = _ORIGINAL_PLAN_COVERAGE(*args, **kwargs)
    elapsed = perf_counter() - start
    links = len(result.candidate_points) * len(result.evaluation_points)
    print(
        "[coverage-performance] "
        f"total={elapsed:.3f}s "
        f"evaluation_points={len(result.evaluation_points)} "
        f"candidates={len(result.candidate_points)} "
        f"candidate_point_pairs={links} "
        f"selected_gateways={len(result.selected_points)}"
    )
    return result


# Patch only internal hot paths. plan_coverage itself is wrapped last so it uses the
# optimized globals above when it executes.
cm.link_margins_db = fast_link_margins_db
cm._spread_sample_points = fast_spread_sample_points
cm.plan_coverage = timed_plan_coverage

# gateway_estimation imports plan_coverage after the patches have been installed,
# so its existing Streamlit cache continues to work normally.
runpy.run_module("gateway_estimation", run_name="__main__")
