import json
import math
import unittest

from coverage_model import (
    RadioConfig,
    augment_gateway_sites,
    coverage_radius_m,
    gateway_sites_geojson,
    parse_geojson,
    plan_coverage,
)


SQUARE = {
    "type": "Feature",
    "properties": {},
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [-96.705, 33.005],
                [-96.695, 33.005],
                [-96.695, 33.015],
                [-96.705, 33.015],
                [-96.705, 33.005],
            ]
        ],
    },
}


class CoverageModelTests(unittest.TestCase):
    def test_geojson_area_is_plausible(self):
        geometry = parse_geojson(json.dumps(SQUARE))
        self.assertGreater(geometry.area_m2, 900_000)
        self.assertLess(geometry.area_m2, 1_200_000)

    def test_container_environment_radius_is_finite(self):
        config = RadioConfig(
            target_sf="SF10",
            path_loss_exponent=3.6,
            additional_loss_db=12,
            fade_margin_db=20,
        )
        radius = coverage_radius_m(config)
        self.assertGreater(radius, 100)
        self.assertLess(radius, 2_000)

    def test_redundancy_two_selects_multiple_gateways(self):
        geometry = parse_geojson(SQUARE)
        config = RadioConfig(
            target_sf="SF10",
            path_loss_exponent=3.6,
            additional_loss_db=12,
            fade_margin_db=20,
        )
        plan = plan_coverage(geometry, config, redundancy=2, resolution_m=150)
        self.assertGreaterEqual(len(plan.selected_points), 2)
        self.assertGreater(plan.coverage_fraction, 0.95)
        self.assertTrue(math.isclose(sum(plan.sf_distribution.values()), 1.0))

    def test_augment_and_export_sites(self):
        geometry = parse_geojson(SQUARE)
        plan = plan_coverage(geometry, RadioConfig(), redundancy=1, resolution_m=200)
        sites = augment_gateway_sites(plan, len(plan.selected_points) + 1)
        self.assertGreaterEqual(len(sites), len(plan.selected_points))
        exported = gateway_sites_geojson(sites, geometry.projection)
        self.assertEqual(exported["type"], "FeatureCollection")
        self.assertEqual(len(exported["features"]), len(sites))


if __name__ == "__main__":
    unittest.main()
