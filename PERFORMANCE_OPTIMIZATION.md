# Performance optimization branch

This branch is intentionally isolated from `main` so the original estimator remains unchanged.

## What is optimized

`streamlit_fast.py` installs two result-preserving hot-path optimizations before loading the existing app:

1. **Shared uplink/downlink link-budget work**: distance, antenna attenuation, obstacle crossings and path loss are calculated once per candidate/device pair instead of twice.
2. **Incremental farthest-point sampling**: `_spread_sample_points` keeps the current minimum distance for every available point instead of recalculating distance to every previously selected point on every iteration.
3. **Non-overlapping azimuth grid**: directional candidates are spaced by one HPBW. Adjacent beams meet at their -3 dB edges, avoiding the previous redundant half-beam orientation grid.

The core planner now caps **physical candidate sites** independently of azimuth count. The same site density is used for omni, sector and directional alternatives; orientation expansion is reported separately. The default physical-site budget is 200.

The launcher also prints one server-side performance line after every uncached coverage optimization:

```text
[coverage-performance] total=2.913s evaluation_points=1842 candidate_sites=200 candidate_orientations=1200 candidate_point_pairs=2210400 selected_sites=3 selected_radios=6
```

This makes it possible to compare identical scenarios without changing the RF result structures or the existing Streamlit UI.

## Run original version

```bash
git checkout main
streamlit run gateway_estimation.py
```

## Run optimized branch

```bash
git checkout performance-optimization
streamlit run streamlit_fast.py
```

## Fair A/B comparison

Use the same polygon/KMZ and the same RF parameters. For each version, restart Streamlit before timing so the first calculation is not served from `st.cache_data`. Record:

- total wall-clock time until the coverage result appears;
- evaluation point count;
- physical candidate-site and orientation counts;
- selected physical-site and radio/antenna counts;
- coverage fraction;
- SF distribution.

The optimized launcher should return the same engineering result. If gateway placement differs only because of an exact distance-score tie, compare coverage fraction, redundancy and RF margins before treating it as a regression.

## Important performance multipliers already present in the application

The path-loss uncertainty analysis can execute the Base, Favorable and Critical coverage plans. Antenna comparison can execute three additional coverage plans. Leave those options disabled while measuring the core optimizer, then enable them to measure realistic worst-case UI latency.

## Next optimization stage

After collecting timings from a real terminal polygon, the next step is vectorizing the candidate × evaluation-point RF matrix with NumPy and separating geometry-dependent data (distance, bearing and obstacle crossings) from radio-parameter-dependent calculations. That is a larger change and should be benchmarked against this branch before merging.
