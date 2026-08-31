"""Geographic coverage planning helpers for the LoRaWAN estimator.

The module intentionally has no GIS dependency. GeoJSON coordinates are projected to
a local equirectangular plane, which is accurate enough for compact industrial sites.
The result is a planning estimate, not a substitute for a calibrated RF survey.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import math
from typing import Iterable
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


EARTH_RADIUS_M = 6_371_000.0

SF_SENSITIVITY_DBM = {
    "SF7": -123.0,
    "SF8": -126.0,
    "SF9": -129.0,
    "SF10": -132.0,
    "SF11": -134.5,
    "SF12": -137.0,
}

ENVIRONMENT_PRESETS = {
    "Área abierta": {
        "path_loss_exponent": 2.4,
        "additional_loss_db": 0.0,
        "fade_margin_db": 10.0,
    },
    "Urbano / industrial": {
        "path_loss_exponent": 3.1,
        "additional_loss_db": 6.0,
        "fade_margin_db": 15.0,
    },
    "Terminal de contenedores": {
        "path_loss_exponent": 3.6,
        "additional_loss_db": 12.0,
        "fade_margin_db": 20.0,
    },
    "Dispositivo dentro de contenedor": {
        "path_loss_exponent": 4.0,
        "additional_loss_db": 25.0,
        "fade_margin_db": 25.0,
    },
}

ANTENNA_PRESETS = {
    "Omnidireccional": {
        "gain_dbi": 6.0,
        "horizontal_beamwidth_deg": 360.0,
        "vertical_beamwidth_deg": 30.0,
        "max_attenuation_db": 25.0,
        "downtilt_deg": 0.0,
    },
    "Sectorial 60° × 35°": {
        "gain_dbi": 12.0,
        "horizontal_beamwidth_deg": 60.0,
        "vertical_beamwidth_deg": 35.0,
        "max_attenuation_db": 25.0,
        "downtilt_deg": 4.0,
    },
    "Direccional 30° × 30°": {
        "gain_dbi": 15.0,
        "horizontal_beamwidth_deg": 30.0,
        "vertical_beamwidth_deg": 30.0,
        "max_attenuation_db": 30.0,
        "downtilt_deg": 4.0,
    },
    "Personalizada": {
        "gain_dbi": 10.0,
        "horizontal_beamwidth_deg": 90.0,
        "vertical_beamwidth_deg": 45.0,
        "max_attenuation_db": 25.0,
        "downtilt_deg": 3.0,
    },
}


@dataclass(frozen=True)
class LocalProjection:
    lon0: float
    lat0: float

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        lat0_rad = math.radians(self.lat0)
        x = EARTH_RADIUS_M * math.radians(lon - self.lon0) * math.cos(lat0_rad)
        y = EARTH_RADIUS_M * math.radians(lat - self.lat0)
        return x, y

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        lat0_rad = math.radians(self.lat0)
        lon = self.lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(lat0_rad)))
        lat = self.lat0 + math.degrees(y / EARTH_RADIUS_M)
        return lon, lat


@dataclass
class ProjectedGeometry:
    polygons: list[list[list[tuple[float, float]]]]
    projection: LocalProjection

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        points = [point for polygon in self.polygons for ring in polygon for point in ring]
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )

    @property
    def area_m2(self) -> float:
        total = 0.0
        for polygon in self.polygons:
            total += abs(_ring_area(polygon[0]))
            total -= sum(abs(_ring_area(hole)) for hole in polygon[1:])
        return max(total, 0.0)

    def contains(self, point: tuple[float, float]) -> bool:
        for polygon in self.polygons:
            if _point_in_ring(point, polygon[0]) and not any(
                _point_in_ring(point, hole) for hole in polygon[1:]
            ):
                return True
        return False


@dataclass(frozen=True)
class RadioConfig:
    frequency_mhz: float = 917.8
    tx_eirp_dbm: float = 20.0
    gateway_gain_dbi: float = 6.0
    gateway_cable_loss_db: float = 2.0
    target_sf: str = "SF10"
    path_loss_exponent: float = 3.6
    additional_loss_db: float = 12.0
    fade_margin_db: float = 20.0

    @property
    def receiver_sensitivity_dbm(self) -> float:
        return SF_SENSITIVITY_DBM[self.target_sf]


@dataclass(frozen=True)
class AntennaConfig:
    antenna_type: str = "Omnidireccional"
    gain_dbi: float = 6.0
    horizontal_beamwidth_deg: float = 360.0
    vertical_beamwidth_deg: float = 30.0
    max_attenuation_db: float = 25.0
    downtilt_deg: float = 0.0
    gateway_height_m: float = 20.0
    device_height_m: float = 1.5

    @property
    def is_omnidirectional(self) -> bool:
        return self.horizontal_beamwidth_deg >= 359.0


@dataclass
class CoveragePlan:
    area_m2: float
    radius_m: float
    evaluation_points: list[tuple[float, float]]
    candidate_points: list[tuple[float, float]]
    candidate_azimuths_deg: list[float]
    selected_indices: list[int]
    redundancy: int
    coverage_fraction: float
    effective_resolution_m: float
    sf_distribution: dict[str, float]
    warnings: list[str]
    antenna_config: AntennaConfig

    @property
    def selected_points(self) -> list[tuple[float, float]]:
        return [self.candidate_points[index] for index in self.selected_indices]

    @property
    def selected_azimuths_deg(self) -> list[float]:
        return [self.candidate_azimuths_deg[index] for index in self.selected_indices]


def parse_geojson(value: str | bytes | dict) -> ProjectedGeometry:
    """Parse Polygon/MultiPolygon GeoJSON into a locally projected geometry."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    data = json.loads(value) if isinstance(value, str) else value
    geometries = list(_iter_polygon_geometries(data))
    if not geometries:
        raise ValueError("El GeoJSON debe contener al menos un Polygon o MultiPolygon.")

    lon_lats = [tuple(point) for polygon in geometries for ring in polygon for point in ring]
    if any(len(point) < 2 for point in lon_lats):
        raise ValueError("Cada coordenada GeoJSON debe contener longitud y latitud.")
    lon0 = sum(point[0] for point in lon_lats) / len(lon_lats)
    lat0 = sum(point[1] for point in lon_lats) / len(lon_lats)
    projection = LocalProjection(lon0=lon0, lat0=lat0)

    projected: list[list[list[tuple[float, float]]]] = []
    for polygon in geometries:
        projected.append(
            [
                [projection.forward(float(point[0]), float(point[1])) for point in ring]
                for ring in polygon
            ]
        )
    geometry = ProjectedGeometry(polygons=projected, projection=projection)
    if geometry.area_m2 <= 0:
        raise ValueError("El polígono no tiene un área válida.")
    return geometry


def parse_kml(value: str | bytes) -> ProjectedGeometry:
    """Parse all Polygon elements from a KML document."""
    if isinstance(value, bytes):
        value = value.decode("utf-8-sig")
    try:
        root = ET.fromstring(value)
    except ET.ParseError as exc:
        raise ValueError(f"El archivo KML no es válido: {exc}") from exc

    polygons = []
    for polygon_element in root.findall(".//{*}Polygon"):
        outer = polygon_element.find(
            "./{*}outerBoundaryIs/{*}LinearRing/{*}coordinates"
        )
        if outer is None or not (outer.text or "").strip():
            continue
        rings = [_parse_kml_coordinates(outer.text or "")]
        for inner in polygon_element.findall(
            "./{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"
        ):
            if (inner.text or "").strip():
                rings.append(_parse_kml_coordinates(inner.text or ""))
        polygons.append(rings)

    if not polygons:
        raise ValueError(
            "El KML/KMZ no contiene polígonos. Verifique que no sea solamente una ruta o marcadores."
        )
    return parse_geojson({"type": "MultiPolygon", "coordinates": polygons})


def parse_kmz(value: bytes) -> ProjectedGeometry:
    """Extract KML documents from a KMZ archive and parse their polygons."""
    try:
        with ZipFile(BytesIO(value)) as archive:
            kml_names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("El KMZ no contiene ningún archivo KML.")
            # doc.kml is the conventional root; otherwise use the first KML.
            selected = next(
                (name for name in kml_names if name.lower().endswith("doc.kml")),
                kml_names[0],
            )
            return parse_kml(archive.read(selected))
    except BadZipFile as exc:
        raise ValueError("El archivo KMZ no es un ZIP/KMZ válido.") from exc


def parse_polygon_file(filename: str, value: bytes) -> ProjectedGeometry:
    """Route a supported polygon file to its parser."""
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension in {"geojson", "json"}:
        return parse_geojson(value)
    if extension == "kml":
        return parse_kml(value)
    if extension == "kmz":
        return parse_kmz(value)
    raise ValueError("Formato no soportado. Use GeoJSON, JSON, KML o KMZ.")


def _parse_kml_coordinates(text: str) -> list[list[float]]:
    coordinates = []
    for token in text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        coordinates.append([float(parts[0]), float(parts[1])])
    if len(coordinates) < 3:
        raise ValueError("Un polígono KML contiene menos de tres coordenadas válidas.")
    if coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    return coordinates


def _iter_polygon_geometries(data: dict) -> Iterable[list[list[list[float]]]]:
    geo_type = data.get("type")
    if geo_type == "FeatureCollection":
        for feature in data.get("features", []):
            yield from _iter_polygon_geometries(feature)
    elif geo_type == "Feature":
        geometry = data.get("geometry") or {}
        yield from _iter_polygon_geometries(geometry)
    elif geo_type == "GeometryCollection":
        for geometry in data.get("geometries", []):
            yield from _iter_polygon_geometries(geometry)
    elif geo_type == "Polygon":
        coordinates = data.get("coordinates") or []
        if coordinates:
            yield coordinates
    elif geo_type == "MultiPolygon":
        for polygon in data.get("coordinates") or []:
            if polygon:
                yield polygon


def _ring_area(ring: list[tuple[float, float]]) -> float:
    if len(ring) < 3:
        return 0.0
    return 0.5 * sum(
        ring[index][0] * ring[(index + 1) % len(ring)][1]
        - ring[(index + 1) % len(ring)][0] * ring[index][1]
        for index in range(len(ring))
    )


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if _point_on_segment(point, previous, current):
            return True
        crosses = (y1 > y) != (y2 > y)
        if crosses:
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
        previous = current
    return inside


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    tolerance: float = 1e-7,
) -> bool:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if abs(cross) > tolerance:
        return False
    return min(x1, x2) - tolerance <= px <= max(x1, x2) + tolerance and min(
        y1, y2
    ) - tolerance <= py <= max(y1, y2) + tolerance


def free_space_path_loss_1m_db(frequency_mhz: float) -> float:
    return 32.44 + 20 * math.log10(frequency_mhz) + 20 * math.log10(0.001)


def coverage_radius_m(config: RadioConfig) -> float:
    available_path_loss = (
        config.tx_eirp_dbm
        + config.gateway_gain_dbi
        - config.gateway_cable_loss_db
        - config.receiver_sensitivity_dbm
        - config.fade_margin_db
        - config.additional_loss_db
    )
    path_loss_1m = free_space_path_loss_1m_db(config.frequency_mhz)
    exponent = max(config.path_loss_exponent, 1.1)
    radius = 10 ** ((available_path_loss - path_loss_1m) / (10 * exponent))
    return min(max(radius, 10.0), 50_000.0)


def received_power_dbm(
    distance_m: float, config: RadioConfig, antenna_attenuation_db: float = 0.0
) -> float:
    distance_m = max(distance_m, 1.0)
    path_loss = (
        free_space_path_loss_1m_db(config.frequency_mhz)
        + 10 * config.path_loss_exponent * math.log10(distance_m)
        + config.additional_loss_db
    )
    return (
        config.tx_eirp_dbm
        + config.gateway_gain_dbi
        - config.gateway_cable_loss_db
        - path_loss
        - antenna_attenuation_db
    )


def bearing_deg(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Bearing in local projected coordinates: 0° north, 90° east."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    return math.degrees(math.atan2(dx, dy)) % 360


def _angle_delta_deg(value: float, reference: float) -> float:
    return (value - reference + 180) % 360 - 180


def antenna_attenuation_db(
    gateway: tuple[float, float],
    device: tuple[float, float],
    azimuth_deg: float,
    antenna: AntennaConfig,
) -> float:
    """Approximate composite H/V pattern using 3 dB beamwidths.

    The quadratic main-lobe approximation reaches 3 dB at half the stated
    beamwidth and is capped at the configured back/side attenuation.
    """
    horizontal_distance = max(math.dist(gateway, device), 1.0)
    if antenna.is_omnidirectional:
        horizontal_attenuation = 0.0
    else:
        horizontal_delta = abs(
            _angle_delta_deg(bearing_deg(gateway, device), azimuth_deg)
        )
        horizontal_attenuation = min(
            12 * (horizontal_delta / antenna.horizontal_beamwidth_deg) ** 2,
            antenna.max_attenuation_db,
        )

    height_delta = max(antenna.gateway_height_m - antenna.device_height_m, 0.0)
    downward_elevation = math.degrees(math.atan2(height_delta, horizontal_distance))
    vertical_delta = downward_elevation - antenna.downtilt_deg
    vertical_attenuation = min(
        12 * (vertical_delta / max(antenna.vertical_beamwidth_deg, 1.0)) ** 2,
        antenna.max_attenuation_db,
    )
    return min(
        horizontal_attenuation + vertical_attenuation,
        antenna.max_attenuation_db,
    )


def link_received_power_dbm(
    gateway: tuple[float, float],
    device: tuple[float, float],
    azimuth_deg: float,
    radio: RadioConfig,
    antenna: AntennaConfig,
) -> float:
    attenuation = antenna_attenuation_db(gateway, device, azimuth_deg, antenna)
    return received_power_dbm(math.dist(gateway, device), radio, attenuation)


def _candidate_azimuths(antenna: AntennaConfig) -> list[float]:
    if antenna.is_omnidirectional:
        return [0.0]
    requested_step = min(max(antenna.horizontal_beamwidth_deg / 2, 15.0), 60.0)
    count = max(1, math.ceil(360 / requested_step))
    step = 360 / count
    return [index * step for index in range(count)]


def _grid_points(
    geometry: ProjectedGeometry,
    requested_spacing_m: float,
    max_points: int,
) -> tuple[list[tuple[float, float]], float]:
    min_x, min_y, max_x, max_y = geometry.bounds
    spacing = max(float(requested_spacing_m), 10.0)
    bbox_area = max((max_x - min_x) * (max_y - min_y), 1.0)
    estimated_points = bbox_area / (spacing * spacing)
    if estimated_points > max_points:
        spacing *= math.sqrt(estimated_points / max_points)

    points: list[tuple[float, float]] = []
    y = min_y + spacing / 2
    row = 0
    while y <= max_y:
        x_offset = spacing / 2 if row % 2 == 0 else spacing
        x = min_x + x_offset
        while x <= max_x:
            point = (x, y)
            if geometry.contains(point):
                points.append(point)
            x += spacing
        y += spacing * math.sqrt(3) / 2
        row += 1

    if not points:
        center = ((min_x + max_x) / 2, (min_y + max_y) / 2)
        if geometry.contains(center):
            points.append(center)
        else:
            points.append(geometry.polygons[0][0][0])
    return points, spacing


def _coverage_sets(
    candidates: list[tuple[float, float]],
    candidate_azimuths_deg: list[float],
    evaluation_points: list[tuple[float, float]],
    radius_m: float,
    radio: RadioConfig,
    antenna: AntennaConfig,
) -> list[list[int]]:
    radius_squared = radius_m * radius_m
    return [
        [
            index
            for index, point in enumerate(evaluation_points)
            if (candidate[0] - point[0]) ** 2 + (candidate[1] - point[1]) ** 2
            <= radius_squared
            and link_received_power_dbm(
                candidate,
                point,
                candidate_azimuths_deg[candidate_index],
                radio,
                antenna,
            )
            >= radio.receiver_sensitivity_dbm + radio.fade_margin_db
        ]
        for candidate_index, candidate in enumerate(candidates)
    ]


def _greedy_multicover(
    coverage_sets: list[list[int]],
    point_count: int,
    redundancy: int,
) -> tuple[list[int], list[int]]:
    remaining = [redundancy] * point_count
    selected: list[int] = []
    available = set(range(len(coverage_sets)))

    while available and any(value > 0 for value in remaining):
        best = max(
            available,
            key=lambda candidate: sum(
                1 for point_index in coverage_sets[candidate] if remaining[point_index] > 0
            ),
        )
        score = sum(1 for point_index in coverage_sets[best] if remaining[point_index] > 0)
        if score == 0:
            break
        selected.append(best)
        available.remove(best)
        for point_index in coverage_sets[best]:
            if remaining[point_index] > 0:
                remaining[point_index] -= 1
    return selected, remaining


def _sf_for_power(received_dbm: float, fade_margin_db: float) -> str:
    for sf_label in ("SF7", "SF8", "SF9", "SF10", "SF11", "SF12"):
        if received_dbm >= SF_SENSITIVITY_DBM[sf_label] + fade_margin_db:
            return sf_label
    return "SF12"


def _derive_sf_distribution(
    evaluation_points: list[tuple[float, float]],
    selected_points: list[tuple[float, float]],
    selected_azimuths_deg: list[float],
    config: RadioConfig,
    antenna: AntennaConfig,
    redundancy: int,
) -> dict[str, float]:
    counts = {sf: 0 for sf in SF_SENSITIVITY_DBM}
    if not selected_points:
        counts["SF12"] = len(evaluation_points)
    else:
        for point in evaluation_points:
            powers = sorted(
                (
                    link_received_power_dbm(
                        gateway,
                        point,
                        selected_azimuths_deg[index],
                        config,
                        antenna,
                    )
                    for index, gateway in enumerate(selected_points)
                ),
                reverse=True,
            )
            rank = min(max(redundancy - 1, 0), len(powers) - 1)
            counts[_sf_for_power(powers[rank], config.fade_margin_db)] += 1
    total = max(sum(counts.values()), 1)
    return {sf: count / total for sf, count in counts.items()}


def plan_coverage(
    geometry: ProjectedGeometry,
    config: RadioConfig,
    antenna: AntennaConfig | None = None,
    redundancy: int = 2,
    resolution_m: float = 100.0,
    max_evaluation_points: int = 3_000,
    max_candidate_points: int = 1_500,
) -> CoveragePlan:
    antenna = antenna or AntennaConfig(gain_dbi=config.gateway_gain_dbi)
    redundancy = min(max(int(redundancy), 1), 3)
    radius_m = coverage_radius_m(config)
    evaluation_points, effective_resolution = _grid_points(
        geometry, resolution_m, max_evaluation_points
    )
    candidate_spacing = max(effective_resolution, min(radius_m / 2, radius_m * 0.75))
    azimuth_options = _candidate_azimuths(antenna)
    max_base_candidates = max(max_candidate_points // len(azimuth_options), 10)
    base_candidate_points, _ = _grid_points(
        geometry, candidate_spacing, max_base_candidates
    )

    candidate_points = []
    candidate_azimuths_deg = []
    for point in base_candidate_points:
        for azimuth in azimuth_options:
            candidate_points.append(point)
            candidate_azimuths_deg.append(azimuth)

    # Evaluation points are valid fallback sites and improve boundary coverage.
    if len(candidate_points) < 3:
        candidate_points = list(dict.fromkeys(candidate_points + evaluation_points))
        candidate_azimuths_deg = [0.0] * len(candidate_points)

    coverage_sets = _coverage_sets(
        candidate_points,
        candidate_azimuths_deg,
        evaluation_points,
        radius_m,
        config,
        antenna,
    )
    selected_indices, remaining = _greedy_multicover(
        coverage_sets, len(evaluation_points), redundancy
    )
    covered = sum(1 for value in remaining if value == 0)
    coverage_fraction = covered / len(evaluation_points)
    selected_points = [candidate_points[index] for index in selected_indices]
    selected_azimuths_deg = [candidate_azimuths_deg[index] for index in selected_indices]
    sf_distribution = _derive_sf_distribution(
        evaluation_points,
        selected_points,
        selected_azimuths_deg,
        config,
        antenna,
        redundancy,
    )
    warnings: list[str] = []
    if effective_resolution > resolution_m * 1.05:
        warnings.append(
            f"La resolución se ajustó automáticamente a {effective_resolution:.0f} m "
            "para limitar el costo de cálculo."
        )
    if coverage_fraction < 0.999:
        warnings.append(
            f"Solo {coverage_fraction:.1%} de los puntos alcanzó redundancia {redundancy}. "
            "Revise el radio, la ubicación permitida de gateways o la resolución."
        )

    return CoveragePlan(
        area_m2=geometry.area_m2,
        radius_m=radius_m,
        evaluation_points=evaluation_points,
        candidate_points=candidate_points,
        candidate_azimuths_deg=candidate_azimuths_deg,
        selected_indices=selected_indices,
        redundancy=redundancy,
        coverage_fraction=coverage_fraction,
        effective_resolution_m=effective_resolution,
        sf_distribution=sf_distribution,
        warnings=warnings,
        antenna_config=antenna,
    )


def augment_gateway_sites(plan: CoveragePlan, target_count: int) -> list[tuple[float, float]]:
    """Add well-separated candidate sites when capacity exceeds coverage count."""
    selected = list(plan.selected_indices)
    available = set(range(len(plan.candidate_points))) - set(selected)
    target_count = min(max(target_count, len(selected)), len(plan.candidate_points))
    while len(selected) < target_count and available:
        if selected:
            best = max(
                available,
                key=lambda index: min(
                    math.dist(plan.candidate_points[index], plan.candidate_points[chosen])
                    for chosen in selected
                ),
            )
        else:
            best = next(iter(available))
        selected.append(best)
        available.remove(best)
    return [plan.candidate_points[index] for index in selected]


def augment_gateway_deployments(
    plan: CoveragePlan, target_count: int
) -> tuple[list[tuple[float, float]], list[float]]:
    """Return points and antenna azimuths for the final capacity/coverage count."""
    selected = list(plan.selected_indices)
    available = set(range(len(plan.candidate_points))) - set(selected)
    target_count = min(max(target_count, len(selected)), len(plan.candidate_points))
    while len(selected) < target_count and available:
        if selected:
            best = max(
                available,
                key=lambda index: min(
                    math.dist(plan.candidate_points[index], plan.candidate_points[chosen])
                    for chosen in selected
                ),
            )
        else:
            best = next(iter(available))
        selected.append(best)
        available.remove(best)
    return (
        [plan.candidate_points[index] for index in selected],
        [plan.candidate_azimuths_deg[index] for index in selected],
    )


def points_to_lon_lat(
    points: Iterable[tuple[float, float]], projection: LocalProjection
) -> list[tuple[float, float]]:
    return [projection.inverse(x, y) for x, y in points]


def gateway_sites_geojson(
    points: Iterable[tuple[float, float]],
    projection: LocalProjection,
    azimuths_deg: Iterable[float] | None = None,
    antenna: AntennaConfig | None = None,
) -> dict:
    points = list(points)
    azimuths = list(azimuths_deg) if azimuths_deg is not None else [0.0] * len(points)
    features = []
    for index, (lon, lat) in enumerate(points_to_lon_lat(points, projection), start=1):
        properties = {
            "gateway_id": f"GW-{index:02d}",
            "azimuth_deg": round(azimuths[index - 1], 1),
        }
        if antenna is not None:
            properties.update(
                {
                    "antenna_type": antenna.antenna_type,
                    "gain_dbi": antenna.gain_dbi,
                    "horizontal_beamwidth_deg": antenna.horizontal_beamwidth_deg,
                    "vertical_beamwidth_deg": antenna.vertical_beamwidth_deg,
                    "downtilt_deg": antenna.downtilt_deg,
                    "gateway_height_m": antenna.gateway_height_m,
                }
            )
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )
    return {"type": "FeatureCollection", "features": features}
