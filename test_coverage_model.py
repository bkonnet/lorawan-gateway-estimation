import json
from io import BytesIO
import math
import unittest
from zipfile import ZipFile

from coverage_model import (
    ANTENNA_PRESETS,
    AntennaConfig,
    RadioConfig,
    antenna_attenuation_db,
    augment_gateway_deployments,
    augment_gateway_sites,
    coverage_radius_m,
    deployment_coverage_counts,
    gateway_sites_geojson,
    parse_geojson,
    parse_kml,
    parse_kmz,
    parse_polygon_file,
    plan_coverage,
    ray_length_within_geometry,
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

SQUARE_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><Polygon>
<outerBoundaryIs><LinearRing><coordinates>
-96.705,33.005,0 -96.695,33.005,0 -96.695,33.015,0
-96.705,33.015,0 -96.705,33.005,0
</coordinates></LinearRing></outerBoundaryIs>
</Polygon></Placemark></Document></kml>"""


class CoverageModelTests(unittest.TestCase):
    def test_geojson_area_is_plausible(self):
        geometry = parse_geojson(json.dumps(SQUARE))
        self.assertGreater(geometry.area_m2, 900_000)
        self.assertLess(geometry.area_m2, 1_200_000)

    def test_kml_area_matches_geojson(self):
        geojson_geometry = parse_geojson(SQUARE)
        kml_geometry = parse_kml(SQUARE_KML)
        self.assertAlmostEqual(kml_geometry.area_m2, geojson_geometry.area_m2, delta=1.0)

    def test_kmz_is_accepted_directly(self):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("doc.kml", SQUARE_KML)
        geometry = parse_kmz(buffer.getvalue())
        routed = parse_polygon_file("terminal.KMZ", buffer.getvalue())
        self.assertAlmostEqual(geometry.area_m2, routed.area_m2, delta=1.0)

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

    def test_sector_pattern_favors_boresight(self):
        preset = ANTENNA_PRESETS["Sectorial 60° × 35°"]
        antenna = AntennaConfig(
            antenna_type="Sectorial 60° × 35°",
            gain_dbi=preset["gain_dbi"],
            horizontal_beamwidth_deg=preset["horizontal_beamwidth_deg"],
            vertical_beamwidth_deg=preset["vertical_beamwidth_deg"],
            max_attenuation_db=preset["max_attenuation_db"],
            downtilt_deg=preset["downtilt_deg"],
        )
        gateway = (0.0, 0.0)
        north = (0.0, 500.0)
        south = (0.0, -500.0)
        self.assertLess(
            antenna_attenuation_db(gateway, north, 0.0, antenna),
            antenna_attenuation_db(gateway, south, 0.0, antenna),
        )

    def test_sector_plan_exports_azimuths(self):
        geometry = parse_geojson(SQUARE)
        preset = ANTENNA_PRESETS["Sectorial 60° × 35°"]
        antenna = AntennaConfig(
            antenna_type="Sectorial 60° × 35°",
            gain_dbi=preset["gain_dbi"],
            horizontal_beamwidth_deg=preset["horizontal_beamwidth_deg"],
            vertical_beamwidth_deg=preset["vertical_beamwidth_deg"],
            max_attenuation_db=preset["max_attenuation_db"],
            downtilt_deg=preset["downtilt_deg"],
        )
        radio = RadioConfig(gateway_gain_dbi=antenna.gain_dbi)
        plan = plan_coverage(
            geometry, radio, antenna=antenna, redundancy=2, resolution_m=150
        )
        points, azimuths = augment_gateway_deployments(
            plan, len(plan.selected_points)
        )
        self.assertEqual(len(points), len(azimuths))
        self.assertEqual(len(points), len(set(points)))
        self.assertTrue(all(0 <= value < 360 for value in azimuths))
        exported = gateway_sites_geojson(
            points, geometry.projection, azimuths, antenna
        )
        self.assertIn("azimuth_deg", exported["features"][0]["properties"])
        self.assertEqual(
            exported["features"][0]["properties"]["antenna_type"],
            "Sectorial 60° × 35°",
        )

    def test_sector_redundancy_uses_distinct_physical_sites(self):
        geometry = parse_geojson(SQUARE)
        preset = ANTENNA_PRESETS["Sectorial 60° × 35°"]
        antenna = AntennaConfig(
            antenna_type="Sectorial 60° × 35°",
            gain_dbi=preset["gain_dbi"],
            horizontal_beamwidth_deg=preset["horizontal_beamwidth_deg"],
            vertical_beamwidth_deg=preset["vertical_beamwidth_deg"],
            max_attenuation_db=preset["max_attenuation_db"],
            downtilt_deg=preset["downtilt_deg"],
        )
        plan = plan_coverage(
            geometry,
            RadioConfig(gateway_gain_dbi=antenna.gain_dbi),
            antenna=antenna,
            redundancy=2,
            resolution_m=100,
            minimum_site_separation_m=150,
        )
        self.assertEqual(len(plan.selected_points), len(set(plan.selected_points)))
        for index, first in enumerate(plan.selected_points):
            for second in plan.selected_points[index + 1 :]:
                self.assertGreaterEqual(math.dist(first, second), 150)

    def test_capacity_augmentation_never_colocates_gateways(self):
        geometry = parse_geojson(SQUARE)
        preset = ANTENNA_PRESETS["Sectorial 60° × 35°"]
        antenna = AntennaConfig(
            antenna_type="Sectorial 60° × 35°",
            gain_dbi=preset["gain_dbi"],
            horizontal_beamwidth_deg=preset["horizontal_beamwidth_deg"],
            vertical_beamwidth_deg=preset["vertical_beamwidth_deg"],
            max_attenuation_db=preset["max_attenuation_db"],
        )
        plan = plan_coverage(
            geometry,
            RadioConfig(gateway_gain_dbi=antenna.gain_dbi),
            antenna=antenna,
            redundancy=1,
            resolution_m=100,
            minimum_site_separation_m=125,
        )
        points, _ = augment_gateway_deployments(plan, 8)
        self.assertEqual(len(points), len(set(points)))
        for index, first in enumerate(points):
            for second in points[index + 1 :]:
                self.assertGreaterEqual(math.dist(first, second), 125)

    def test_capacity_sector_azimuths_favor_polygon_interior(self):
        geometry = parse_geojson(SQUARE)
        preset = ANTENNA_PRESETS["Sectorial 60° × 35°"]
        antenna = AntennaConfig(
            antenna_type="Sectorial 60° × 35°",
            gain_dbi=preset["gain_dbi"],
            horizontal_beamwidth_deg=preset["horizontal_beamwidth_deg"],
            vertical_beamwidth_deg=preset["vertical_beamwidth_deg"],
            max_attenuation_db=preset["max_attenuation_db"],
        )
        plan = plan_coverage(
            geometry,
            RadioConfig(gateway_gain_dbi=antenna.gain_dbi),
            antenna=antenna,
            redundancy=1,
            resolution_m=100,
            minimum_site_separation_m=125,
        )
        points, azimuths = augment_gateway_deployments(plan, 8)
        for point, azimuth in zip(points, azimuths):
            arrow_length = ray_length_within_geometry(
                geometry, point, azimuth, plan.radius_m * 0.2
            )
            self.assertGreater(arrow_length, 25)

    def test_map_arrow_is_clipped_inside_polygon(self):
        geometry = parse_geojson(SQUARE)
        plan = plan_coverage(geometry, RadioConfig(), redundancy=1, resolution_m=100)
        point = plan.evaluation_points[len(plan.evaluation_points) // 2]
        length = ray_length_within_geometry(geometry, point, 90.0, 10_000)
        endpoint = (point[0] + length * 0.999, point[1])
        self.assertTrue(geometry.contains(endpoint))

    def test_final_deployment_coverage_is_verified_point_by_point(self):
        geometry = parse_geojson(SQUARE)
        plan = plan_coverage(
            geometry,
            RadioConfig(),
            redundancy=2,
            resolution_m=100,
            minimum_site_separation_m=100,
        )
        points, azimuths = augment_gateway_deployments(
            plan, len(plan.selected_points) + 2
        )
        counts = deployment_coverage_counts(plan, points, azimuths)
        self.assertEqual(len(counts), len(plan.evaluation_points))
        self.assertTrue(all(count >= 0 for count in counts))
        verified_fraction = sum(count >= 2 for count in counts) / len(counts)
        self.assertGreaterEqual(verified_fraction, plan.coverage_fraction)

    def test_isotropic_reference_radius_is_below_directional_boresight(self):
        geometry = parse_geojson(SQUARE)
        preset = ANTENNA_PRESETS["Direccional 30° × 30°"]
        antenna = AntennaConfig(
            antenna_type="Direccional 30° × 30°",
            gain_dbi=preset["gain_dbi"],
            horizontal_beamwidth_deg=preset["horizontal_beamwidth_deg"],
            vertical_beamwidth_deg=preset["vertical_beamwidth_deg"],
            max_attenuation_db=preset["max_attenuation_db"],
        )
        plan = plan_coverage(
            geometry,
            RadioConfig(gateway_gain_dbi=antenna.gain_dbi),
            antenna=antenna,
            redundancy=1,
            resolution_m=150,
        )
        self.assertLess(plan.isotropic_radius_m, plan.radius_m)

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
