"""Geographic coverage planning helpers for the LoRaWAN estimator.

The module intentionally has no GIS dependency. GeoJSON coordinates are projected to
a local equirectangular plane, which is accurate enough for compact industrial sites.
The result is a planning estimate, not a substitute for a calibrated RF survey.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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

    @property
    def polygon_bounds(self) -> list[tuple[float, float, float, float]]:
        return [
            (
                min(point[0] for ring in polygon for point in ring),
                min(point[1] for ring in polygon for point in ring),
                max(point[0] for ring in polygon for point in ring),
                max(point[1] for ring in polygon for point in ring),
            )
            for polygon in self.polygons
        ]


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
    device_antenna_gain_dbi: float = 0.0
    device_installation_loss_db: float = 0.0
    gateway_tx_eirp_dbm: float = 30.0
    device_receiver_sensitivity_dbm: float = -129.0
    validate_downlink: bool = True
    sf_sensitivities_dbm: tuple[float, ...] = tuple(SF_SENSITIVITY_DBM.values())
    obstacle_loss_db: float = 0.0
    maximum_obstacle_loss_db: float = 40.0

    @property
    def receiver_sensitivity_dbm(self) -> float:
        return self.sensitivity_for_sf(self.target_sf)

    def sensitivity_for_sf(self, sf_label: str) -> float:
        labels = tuple(SF_SENSITIVITY_DBM)
        if len(self.sf_sensitivities_dbm) != len(labels):
            raise ValueError("Debe configurar una sensibilidad para cada SF7-SF12.")
        return float(self.sf_sensitivities_dbm[labels.index(sf_label)])


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
    isotropic_radius_m: float
    evaluation_points: list[tuple[float, float]]
    evaluation_weights: list[float]
    boundary_point_count: int
    candidate_points: list[tuple[float, float]]
    candidate_azimuths_deg: list[float]
    candidate_site_ids: list[int]
    candidate_coverage_counts: list[int]
    candidate_quality_scores: list[float]
    selected_indices: list[int]
    redundancy: int
    require_hpbw_redundancy: bool
    coverage_fraction: float
    effective_resolution_m: float
    sf_distribution: dict[str, float]
    warnings: list[str]
    antenna_config: AntennaConfig
    radio_config: RadioConfig
    minimum_site_separation_m: float
    edge_priority: float
    dispersion_weight: float
    obstacles: ProjectedGeometry | None = None

    @property
    def selected_points(self) -> list[tuple[float, float]]:
        return [self.candidate_points[index] for index in self.selected_indices]

    @property
    def selected_azimuths_deg(self) -> list[float]:
        return [self.candidate_azimuths_deg[index] for index in self.selected_indices]


def parse_geojson(
    value: str | bytes | dict,
    projection: LocalProjection | None = None,
) -> ProjectedGeometry:
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
    if projection is None:
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


def parse_kml(
    value: str | bytes,
    projection: LocalProjection | None = None,
) -> ProjectedGeometry:
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
    return parse_geojson(
        {"type": "MultiPolygon", "coordinates": polygons},
        projection=projection,
    )


def parse_kmz(
    value: bytes,
    projection: LocalProjection | None = None,
) -> ProjectedGeometry:
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
            return parse_kml(archive.read(selected), projection=projection)
    except BadZipFile as exc:
        raise ValueError("El archivo KMZ no es un ZIP/KMZ válido.") from exc


def parse_polygon_file(
    filename: str,
    value: bytes,
    projection: LocalProjection | None = None,
) -> ProjectedGeometry:
    """Route a supported polygon file to its parser."""
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension in {"geojson", "json"}:
        return parse_geojson(value, projection=projection)
    if extension == "kml":
        return parse_kml(value, projection=projection)
    if extension == "kmz":
        return parse_kmz(value, projection=projection)
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


def _orientation(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (
        second[1] - first[1]
    ) * (third[0] - first[0])


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    o1 = _orientation(first_start, first_end, second_start)
    o2 = _orientation(first_start, first_end, second_end)
    o3 = _orientation(second_start, second_end, first_start)
    o4 = _orientation(second_start, second_end, first_end)
    tolerance = 1e-7
    if ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance)) and (
        (o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance)
    ):
        return True
    return (
        (abs(o1) <= tolerance and _point_on_segment(second_start, first_start, first_end))
        or (abs(o2) <= tolerance and _point_on_segment(second_end, first_start, first_end))
        or (abs(o3) <= tolerance and _point_on_segment(first_start, second_start, second_end))
        or (abs(o4) <= tolerance and _point_on_segment(first_end, second_start, second_end))
    )


def obstacle_crossing_count(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: ProjectedGeometry | None,
) -> int:
    """Count distinct obstacle polygons intersected by a radio path."""
    if obstacles is None:
        return 0
    segment_bounds = (
        min(start[0], end[0]),
        min(start[1], end[1]),
        max(start[0], end[0]),
        max(start[1], end[1]),
    )
    count = 0
    for polygon, bounds in zip(obstacles.polygons, obstacles.polygon_bounds):
        if (
            segment_bounds[2] < bounds[0]
            or segment_bounds[0] > bounds[2]
            or segment_bounds[3] < bounds[1]
            or segment_bounds[1] > bounds[3]
        ):
            continue
        outer = polygon[0]
        intersects = _point_in_ring(start, outer) or _point_in_ring(end, outer)
        if not intersects:
            intersects = any(
                _segments_intersect(start, end, outer[index - 1], outer[index])
                for index in range(len(outer))
            )
        if intersects:
            count += 1
    return count


def obstacle_attenuation_db(
    start: tuple[float, float],
    end: tuple[float, float],
    config: RadioConfig,
    obstacles: ProjectedGeometry | None,
) -> float:
    return min(
        obstacle_crossing_count(start, end, obstacles) * config.obstacle_loss_db,
        config.maximum_obstacle_loss_db,
    )


def coverage_radius_m(config: RadioConfig) -> float:
    uplink_available_path_loss = (
        config.tx_eirp_dbm
        + config.gateway_gain_dbi
        - config.gateway_cable_loss_db
        - config.receiver_sensitivity_dbm
        - config.fade_margin_db
        - config.additional_loss_db
        - config.device_installation_loss_db
        + config.device_antenna_gain_dbi
    )
    downlink_available_path_loss = (
        config.gateway_tx_eirp_dbm
        + config.device_antenna_gain_dbi
        - config.device_installation_loss_db
        - config.device_receiver_sensitivity_dbm
        - config.fade_margin_db
        - config.additional_loss_db
    )
    available_path_loss = (
        min(uplink_available_path_loss, downlink_available_path_loss)
        if config.validate_downlink
        else uplink_available_path_loss
    )
    path_loss_1m = free_space_path_loss_1m_db(config.frequency_mhz)
    exponent = max(config.path_loss_exponent, 1.1)
    radius = 10 ** ((available_path_loss - path_loss_1m) / (10 * exponent))
    return min(max(radius, 10.0), 50_000.0)


def path_loss_exponent_scenarios(
    base_exponent: float,
    variation: float = 0.3,
) -> list[tuple[str, float]]:
    """Build favorable/base/critical exponents around the selected estimate."""
    base = min(max(float(base_exponent), 1.1), 6.0)
    spread = min(max(float(variation), 0.0), 2.0)
    return [
        ("Favorable", max(base - spread, 1.1)),
        ("Base", base),
        ("Crítico", min(base + spread, 6.0)),
    ]


def received_power_dbm(
    distance_m: float,
    config: RadioConfig,
    antenna_attenuation_db: float = 0.0,
    obstacle_attenuation_db: float = 0.0,
) -> float:
    distance_m = max(distance_m, 1.0)
    path_loss = (
        free_space_path_loss_1m_db(config.frequency_mhz)
        + 10 * config.path_loss_exponent * math.log10(distance_m)
        + config.additional_loss_db
    )
    return (
        config.tx_eirp_dbm
        + config.device_antenna_gain_dbi
        + config.gateway_gain_dbi
        - config.gateway_cable_loss_db
        - config.device_installation_loss_db
        - path_loss
        - antenna_attenuation_db
        - obstacle_attenuation_db
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


def link_within_horizontal_hpbw(
    gateway: tuple[float, float],
    device: tuple[float, float],
    azimuth_deg: float,
    antenna: AntennaConfig,
) -> bool:
    """Return whether a link lies inside the antenna's horizontal half-power beam."""
    if antenna.is_omnidirectional:
        return True
    horizontal_delta = abs(
        _angle_delta_deg(bearing_deg(gateway, device), azimuth_deg)
    )
    return horizontal_delta <= antenna.horizontal_beamwidth_deg / 2.0 + 1e-9


def link_received_power_dbm(
    gateway: tuple[float, float],
    device: tuple[float, float],
    azimuth_deg: float,
    radio: RadioConfig,
    antenna: AntennaConfig,
    obstacles: ProjectedGeometry | None = None,
) -> float:
    attenuation = antenna_attenuation_db(gateway, device, azimuth_deg, antenna)
    obstacle_loss = obstacle_attenuation_db(gateway, device, radio, obstacles)
    return received_power_dbm(
        math.dist(gateway, device), radio, attenuation, obstacle_loss
    )


def downlink_received_power_dbm(
    gateway: tuple[float, float],
    device: tuple[float, float],
    azimuth_deg: float,
    radio: RadioConfig,
    antenna: AntennaConfig,
    obstacles: ProjectedGeometry | None = None,
) -> float:
    distance_m = max(math.dist(gateway, device), 1.0)
    path_loss = (
        free_space_path_loss_1m_db(radio.frequency_mhz)
        + 10 * radio.path_loss_exponent * math.log10(distance_m)
        + radio.additional_loss_db
    )
    return (
        radio.gateway_tx_eirp_dbm
        + radio.device_antenna_gain_dbi
        - radio.device_installation_loss_db
        - path_loss
        - antenna_attenuation_db(gateway, device, azimuth_deg, antenna)
        - obstacle_attenuation_db(gateway, device, radio, obstacles)
    )


def link_margins_db(
    gateway: tuple[float, float],
    device: tuple[float, float],
    azimuth_deg: float,
    radio: RadioConfig,
    antenna: AntennaConfig,
    obstacles: ProjectedGeometry | None = None,
) -> tuple[float, float, float]:
    """Return uplink, downlink and limiting margins above receiver sensitivity."""
    uplink_margin = (
        link_received_power_dbm(
            gateway, device, azimuth_deg, radio, antenna, obstacles
        )
        - radio.receiver_sensitivity_dbm
    )
    downlink_margin = (
        downlink_received_power_dbm(
            gateway, device, azimuth_deg, radio, antenna, obstacles
        )
        - radio.device_receiver_sensitivity_dbm
    )
    limiting_margin = (
        min(uplink_margin, downlink_margin)
        if radio.validate_downlink
        else uplink_margin
    )
    return uplink_margin, downlink_margin, limiting_margin


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
    evaluation_weights: list[float],
    radius_m: float,
    radio: RadioConfig,
    antenna: AntennaConfig,
    require_hpbw_redundancy: bool,
    obstacles: ProjectedGeometry | None = None,
) -> tuple[list[list[int]], list[float]]:
    radius_squared = radius_m * radius_m
    coverage_sets: list[list[int]] = []
    quality_scores: list[float] = []
    for candidate_index, candidate in enumerate(candidates):
        covered_points: list[int] = []
        quality = 0.0
        for point_index, point in enumerate(evaluation_points):
            if (
                (candidate[0] - point[0]) ** 2
                + (candidate[1] - point[1]) ** 2
                > radius_squared
            ):
                continue
            if require_hpbw_redundancy and not link_within_horizontal_hpbw(
                candidate,
                point,
                candidate_azimuths_deg[candidate_index],
                antenna,
            ):
                continue
            _, _, limiting_margin_db = link_margins_db(
                candidate,
                point,
                candidate_azimuths_deg[candidate_index],
                radio,
                antenna,
                obstacles,
            )
            design_surplus_db = limiting_margin_db - radio.fade_margin_db
            if design_surplus_db >= 0:
                covered_points.append(point_index)
                # Prefer an orientation whose main lobe provides useful margin
                # inside the polygon, even if several azimuths cover the same count.
                quality += evaluation_weights[point_index] * (
                    1.0 + min(design_surplus_db, 40.0)
                )
        coverage_sets.append(covered_points)
        quality_scores.append(quality)
    return coverage_sets, quality_scores


def _greedy_multicover(
    coverage_sets: list[list[int]],
    candidate_quality_scores: list[float],
    candidate_points: list[tuple[float, float]],
    candidate_site_ids: list[int],
    evaluation_weights: list[float],
    point_count: int,
    redundancy: int,
    minimum_site_separation_m: float,
    dispersion_weight: float,
    dispersion_scale_m: float,
) -> tuple[list[int], list[int]]:
    remaining = [redundancy] * point_count
    selected: list[int] = []
    available = set(range(len(coverage_sets)))

    while available and any(value > 0 for value in remaining):
        def selection_score(candidate: int) -> tuple[float, float, int]:
            useful_points = [
                point_index
                for point_index in coverage_sets[candidate]
                if remaining[point_index] > 0
            ]
            weighted_coverage = sum(
                evaluation_weights[point_index] for point_index in useful_points
            )
            if selected and dispersion_weight > 0:
                minimum_distance = min(
                    math.dist(candidate_points[candidate], candidate_points[chosen])
                    for chosen in selected
                )
                dispersion_ratio = min(
                    minimum_distance / max(dispersion_scale_m, 1.0), 1.0
                )
                weighted_coverage *= 1.0 + dispersion_weight * dispersion_ratio
            return (
                weighted_coverage,
                candidate_quality_scores[candidate],
                len(useful_points),
            )

        best = max(available, key=selection_score)
        score = sum(1 for point_index in coverage_sets[best] if remaining[point_index] > 0)
        if score == 0:
            break
        selected.append(best)
        selected_site = candidate_site_ids[best]
        selected_point = candidate_points[best]
        blocked_sites = {
            candidate_site_ids[index]
            for index in available
            if candidate_site_ids[index] == selected_site
            or math.dist(candidate_points[index], selected_point)
            < minimum_site_separation_m
        }
        available = {
            index
            for index in available
            if candidate_site_ids[index] not in blocked_sites
        }
        for point_index in coverage_sets[best]:
            if remaining[point_index] > 0:
                remaining[point_index] -= 1
    return selected, remaining


def _sf_for_power(
    received_dbm: float,
    fade_margin_db: float,
    config: RadioConfig,
) -> str:
    for sf_label in ("SF7", "SF8", "SF9", "SF10", "SF11", "SF12"):
        if received_dbm >= config.sensitivity_for_sf(sf_label) + fade_margin_db:
            return sf_label
    return "SF12"


def _derive_sf_distribution(
    evaluation_points: list[tuple[float, float]],
    selected_points: list[tuple[float, float]],
    selected_azimuths_deg: list[float],
    config: RadioConfig,
    antenna: AntennaConfig,
    redundancy: int,
    require_hpbw_redundancy: bool,
    obstacles: ProjectedGeometry | None = None,
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
                        obstacles,
                    )
                    for index, gateway in enumerate(selected_points)
                    if not require_hpbw_redundancy
                    or link_within_horizontal_hpbw(
                        gateway,
                        point,
                        selected_azimuths_deg[index],
                        antenna,
                    )
                ),
                reverse=True,
            )
            if not powers:
                counts["SF12"] += 1
                continue
            rank = min(max(redundancy - 1, 0), len(powers) - 1)
            counts[_sf_for_power(powers[rank], config.fade_margin_db, config)] += 1
    total = max(sum(counts.values()), 1)
    return {sf: count / total for sf, count in counts.items()}


def deployment_link_analysis(
    plan: CoveragePlan,
    gateway_points: list[tuple[float, float]],
    gateway_azimuths_deg: list[float],
    evaluation_points: Iterable[tuple[float, float]] | None = None,
) -> list[dict[str, float | int]]:
    """Return point-level redundant link margins for a deployed network."""
    if len(gateway_points) != len(gateway_azimuths_deg):
        raise ValueError("Cada gateway debe tener exactamente un azimut.")
    points_to_evaluate = (
        plan.evaluation_points
        if evaluation_points is None
        else list(evaluation_points)
    )
    radius_squared = plan.radius_m * plan.radius_m
    analysis: list[dict[str, float | int]] = []
    for point in points_to_evaluate:
        links: list[tuple[float, float, float, bool]] = []
        for gateway, azimuth in zip(gateway_points, gateway_azimuths_deg):
            if (
                (gateway[0] - point[0]) ** 2 + (gateway[1] - point[1]) ** 2
                > radius_squared
            ):
                continue
            links.append(
                (
                    *link_margins_db(
                        gateway,
                        point,
                        azimuth,
                        plan.radio_config,
                        plan.antenna_config,
                        plan.obstacles,
                    ),
                    link_within_horizontal_hpbw(
                        gateway, point, azimuth, plan.antenna_config
                    ),
                )
            )
        radio_count = sum(
            limiting >= plan.radio_config.fade_margin_db
            for _, _, limiting, _ in links
        )
        hpbw_count = sum(
            limiting >= plan.radio_config.fade_margin_db and within_hpbw
            for _, _, limiting, within_hpbw in links
        )
        eligible_links = [
            link
            for link in links
            if not plan.require_hpbw_redundancy or link[3]
        ]
        eligible_links.sort(key=lambda item: item[2], reverse=True)
        count = hpbw_count if plan.require_hpbw_redundancy else radio_count
        rank = plan.redundancy - 1
        if len(eligible_links) > rank:
            uplink_margin, downlink_margin, limiting_margin, _ = eligible_links[rank]
            design_surplus = limiting_margin - plan.radio_config.fade_margin_db
        else:
            uplink_margin = downlink_margin = limiting_margin = design_surplus = -math.inf
        analysis.append(
            {
                "count": count,
                "radio_count": radio_count,
                "hpbw_count": hpbw_count,
                "uplink_margin_db": uplink_margin,
                "downlink_margin_db": downlink_margin,
                "limiting_margin_db": limiting_margin,
                "design_surplus_db": design_surplus,
            }
        )
    return analysis


def deployment_coverage_counts(
    plan: CoveragePlan,
    gateway_points: list[tuple[float, float]],
    gateway_azimuths_deg: list[float],
) -> list[int]:
    """Count distinct deployed gateways covering every evaluation point."""
    return [
        int(item["count"])
        for item in deployment_link_analysis(
            plan, gateway_points, gateway_azimuths_deg
        )
    ]


def _spread_sample_points(
    points: list[tuple[float, float]], max_count: int
) -> list[tuple[float, float]]:
    """Limit candidate sites while preserving geographic dispersion."""
    unique_points = list(dict.fromkeys(points))
    if len(unique_points) <= max_count:
        return unique_points

    center = (
        sum(point[0] for point in unique_points) / len(unique_points),
        sum(point[1] for point in unique_points) / len(unique_points),
    )
    first = min(range(len(unique_points)), key=lambda index: math.dist(unique_points[index], center))
    selected = [first]
    available = set(range(len(unique_points))) - {first}
    while available and len(selected) < max_count:
        best = max(
            available,
            key=lambda index: min(
                math.dist(unique_points[index], unique_points[chosen])
                for chosen in selected
            ),
        )
        selected.append(best)
        available.remove(best)
    return [unique_points[index] for index in selected]


def _sample_boundary_points(
    geometry: ProjectedGeometry,
    requested_spacing_m: float,
    max_points: int,
) -> list[tuple[float, float]]:
    """Sample every outer edge and hole boundary so narrow/extreme areas count."""
    spacing = max(float(requested_spacing_m), 5.0)
    points: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for polygon in geometry.polygons:
        for ring in polygon:
            for start, end in zip(ring, ring[1:]):
                segment_length = math.dist(start, end)
                segments = max(1, math.ceil(segment_length / spacing))
                for offset in range(segments):
                    ratio = offset / segments
                    point = (
                        start[0] + (end[0] - start[0]) * ratio,
                        start[1] + (end[1] - start[1]) * ratio,
                    )
                    key = (round(point[0], 5), round(point[1], 5))
                    if key not in seen:
                        seen.add(key)
                        points.append(point)
    return _spread_sample_points(points, max_points)


def _targeted_candidate_points(
    geometry: ProjectedGeometry,
    targets: list[tuple[float, float]],
    radius_m: float,
    minimum_site_separation_m: float,
    max_points: int,
) -> list[tuple[float, float]]:
    """Create extra in-polygon sites around points left deficient by the first pass."""
    if not targets or max_points <= 0:
        return []
    ring_distance = min(
        max(radius_m * 0.35, minimum_site_separation_m * 0.75, 10.0),
        radius_m * 0.85,
    )
    points: list[tuple[float, float]] = []
    for target in targets:
        if geometry.contains(target):
            points.append(target)
        for angle_deg in range(0, 360, 30):
            angle = math.radians(angle_deg)
            candidate = (
                target[0] + math.sin(angle) * ring_distance,
                target[1] + math.cos(angle) * ring_distance,
            )
            if geometry.contains(candidate):
                points.append(candidate)
    return _spread_sample_points(points, max_points)


def ray_length_within_geometry(
    geometry: ProjectedGeometry,
    start: tuple[float, float],
    azimuth_deg: float,
    maximum_length_m: float,
) -> float:
    """Return the first in-polygon ray length, useful for map arrows."""
    maximum_length_m = max(float(maximum_length_m), 0.0)
    if maximum_length_m == 0:
        return 0.0
    angle = math.radians(azimuth_deg)

    def point_at(distance: float) -> tuple[float, float]:
        return (
            start[0] + math.sin(angle) * distance,
            start[1] + math.cos(angle) * distance,
        )

    step = max(maximum_length_m / 80.0, 1.0)
    # Candidate sites may lie exactly on a sampled polygon boundary. Depending
    # on the ray-casting parity that coordinate can be classified as outside,
    # so inspect a small point along the boresight before rejecting the arrow.
    if not geometry.contains(start) and not geometry.contains(
        point_at(min(step * 0.25, 0.5))
    ):
        return 0.0
    last_inside = 0.0
    distance = step
    while distance <= maximum_length_m:
        if not geometry.contains(point_at(distance)):
            low, high = last_inside, distance
            for _ in range(16):
                middle = (low + high) / 2
                if geometry.contains(point_at(middle)):
                    low = middle
                else:
                    high = middle
            return low
        last_inside = distance
        distance += step
    return maximum_length_m


def plan_coverage(
    geometry: ProjectedGeometry,
    config: RadioConfig,
    antenna: AntennaConfig | None = None,
    redundancy: int = 2,
    resolution_m: float = 100.0,
    minimum_site_separation_m: float = 100.0,
    edge_priority: float = 3.0,
    dispersion_weight: float = 0.30,
    obstacles: ProjectedGeometry | None = None,
    max_evaluation_points: int = 3_000,
    max_candidate_points: int = 1_500,
    require_hpbw_redundancy: bool = True,
) -> CoveragePlan:
    antenna = antenna or AntennaConfig(gain_dbi=config.gateway_gain_dbi)
    redundancy = min(max(int(redundancy), 1), 3)
    minimum_site_separation_m = max(float(minimum_site_separation_m), 0.0)
    edge_priority = max(float(edge_priority), 1.0)
    dispersion_weight = min(max(float(dispersion_weight), 0.0), 1.0)
    radius_m = coverage_radius_m(config)
    # Treat the requested resolution as the largest design cell and verify at
    # twice that density. This prevents a narrow gap between visible samples
    # from being accepted as fully covered.
    verification_spacing_m = max(float(resolution_m) / 2.0, 10.0)
    boundary_budget = max(min(max_evaluation_points // 3, 1_000), 100)
    interior_budget = max(max_evaluation_points - boundary_budget, 100)
    interior_evaluation_points, effective_resolution = _grid_points(
        geometry, verification_spacing_m, interior_budget
    )
    boundary_points = _sample_boundary_points(
        geometry,
        max(effective_resolution / 2.0, 10.0),
        boundary_budget,
    )
    evaluation_points = interior_evaluation_points + boundary_points
    evaluation_weights = [1.0] * len(interior_evaluation_points) + [
        edge_priority
    ] * len(boundary_points)
    candidate_spacing = max(effective_resolution, min(radius_m / 2, radius_m * 0.75))
    azimuth_options = _candidate_azimuths(antenna)
    max_base_candidates = max(max_candidate_points // len(azimuth_options), 10)
    base_candidate_points, _ = _grid_points(
        geometry, candidate_spacing, max_base_candidates
    )
    physical_candidate_points = _spread_sample_points(
        base_candidate_points + interior_evaluation_points + boundary_points,
        max_base_candidates,
    )

    def solve_with_sites(candidate_sites):
        candidate_points = []
        candidate_azimuths_deg = []
        candidate_site_ids = []
        minimum_inward_boresight_m = min(
            max(effective_resolution / 2.0, 25.0),
            radius_m * 0.20,
        )
        for site_id, point in enumerate(candidate_sites):
            for azimuth in azimuth_options:
                if (
                    not antenna.is_omnidirectional
                    and ray_length_within_geometry(
                        geometry,
                        point,
                        azimuth,
                        radius_m * 0.20,
                    )
                    <= minimum_inward_boresight_m
                ):
                    continue
                candidate_points.append(point)
                candidate_azimuths_deg.append(azimuth)
                candidate_site_ids.append(site_id)
        coverage_sets, candidate_quality_scores = _coverage_sets(
            candidate_points,
            candidate_azimuths_deg,
            evaluation_points,
            evaluation_weights,
            radius_m,
            config,
            antenna,
            require_hpbw_redundancy,
            obstacles,
        )
        selected_indices, remaining = _greedy_multicover(
            coverage_sets,
            candidate_quality_scores,
            candidate_points,
            candidate_site_ids,
            evaluation_weights,
            len(evaluation_points),
            redundancy,
            minimum_site_separation_m,
            dispersion_weight,
            radius_m,
        )
        return (
            candidate_points,
            candidate_azimuths_deg,
            candidate_site_ids,
            coverage_sets,
            candidate_quality_scores,
            selected_indices,
            remaining,
        )

    (
        candidate_points,
        candidate_azimuths_deg,
        candidate_site_ids,
        coverage_sets,
        candidate_quality_scores,
        selected_indices,
        remaining,
    ) = solve_with_sites(physical_candidate_points)

    if any(value > 0 for value in remaining):
        deficient_points = [
            evaluation_points[index]
            for index, value in enumerate(remaining)
            if value > 0
        ]
        targeted_sites = _targeted_candidate_points(
            geometry,
            deficient_points,
            radius_m,
            minimum_site_separation_m,
            max_base_candidates,
        )
        refined_site_limit = min(max_base_candidates * 2, max_candidate_points)
        refined_sites = _spread_sample_points(
            physical_candidate_points + targeted_sites + boundary_points,
            refined_site_limit,
        )
        refined_result = solve_with_sites(refined_sites)
        refined_remaining = refined_result[-1]
        initial_covered = sum(value == 0 for value in remaining)
        refined_covered = sum(value == 0 for value in refined_remaining)
        if refined_covered > initial_covered or (
            refined_covered == initial_covered
            and len(refined_result[-2]) < len(selected_indices)
        ):
            (
                candidate_points,
                candidate_azimuths_deg,
                candidate_site_ids,
                coverage_sets,
                candidate_quality_scores,
                selected_indices,
                remaining,
            ) = refined_result
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
        require_hpbw_redundancy,
        obstacles,
    )
    warnings: list[str] = []
    if effective_resolution > verification_spacing_m * 1.05:
        warnings.append(
            f"La resolución se ajustó automáticamente a {effective_resolution:.0f} m "
            "para limitar el costo de cálculo."
        )
    if coverage_fraction < 0.999:
        criterion = (
            f" redundancia robusta {redundancy}× dentro del HPBW"
            if require_hpbw_redundancy and not antenna.is_omnidirectional
            else f" redundancia {redundancy}"
        )
        warnings.append(
            f"Solo {coverage_fraction:.1%} de los puntos alcanzó{criterion}. "
            "Revise el radio, la ubicación permitida de gateways o la resolución."
        )

    return CoveragePlan(
        area_m2=geometry.area_m2,
        radius_m=radius_m,
        isotropic_radius_m=coverage_radius_m(
            replace(config, gateway_gain_dbi=0.0)
        ),
        evaluation_points=evaluation_points,
        evaluation_weights=evaluation_weights,
        boundary_point_count=len(boundary_points),
        candidate_points=candidate_points,
        candidate_azimuths_deg=candidate_azimuths_deg,
        candidate_site_ids=candidate_site_ids,
        candidate_coverage_counts=[len(items) for items in coverage_sets],
        candidate_quality_scores=candidate_quality_scores,
        selected_indices=selected_indices,
        redundancy=redundancy,
        require_hpbw_redundancy=require_hpbw_redundancy,
        coverage_fraction=coverage_fraction,
        effective_resolution_m=effective_resolution,
        sf_distribution=sf_distribution,
        warnings=warnings,
        antenna_config=antenna,
        radio_config=config,
        minimum_site_separation_m=minimum_site_separation_m,
        edge_priority=edge_priority,
        dispersion_weight=dispersion_weight,
        obstacles=obstacles,
    )


def augment_gateway_sites(plan: CoveragePlan, target_count: int) -> list[tuple[float, float]]:
    """Add well-separated candidate sites when capacity exceeds coverage count."""
    points, _ = augment_gateway_deployments(plan, target_count)
    return points


def augment_gateway_deployments(
    plan: CoveragePlan, target_count: int
) -> tuple[list[tuple[float, float]], list[float]]:
    """Return points and antenna azimuths for the final capacity/coverage count."""
    selected = list(plan.selected_indices)
    selected_site_ids = {plan.candidate_site_ids[index] for index in selected}
    candidates_by_site: dict[int, list[int]] = {}
    for index, site_id in enumerate(plan.candidate_site_ids):
        candidates_by_site.setdefault(site_id, []).append(index)

    available_sites = set(candidates_by_site) - selected_site_ids
    target_count = min(max(target_count, len(selected)), len(candidates_by_site))
    while len(selected) < target_count and available_sites:
        eligible_sites = {
            site_id
            for site_id in available_sites
            if all(
                math.dist(
                    plan.candidate_points[candidates_by_site[site_id][0]],
                    plan.candidate_points[chosen],
                )
                >= plan.minimum_site_separation_m
                for chosen in selected
            )
        }
        if not eligible_sites:
            break
        if selected:
            best_site = max(
                eligible_sites,
                key=lambda site_id: min(
                    math.dist(
                        plan.candidate_points[candidates_by_site[site_id][0]],
                        plan.candidate_points[chosen],
                    )
                    for chosen in selected
                ),
            )
        else:
            best_site = next(iter(eligible_sites))
        best = max(
            candidates_by_site[best_site],
            key=lambda index: (
                plan.candidate_coverage_counts[index],
                plan.candidate_quality_scores[index],
            ),
        )
        selected.append(best)
        selected_site_ids.add(best_site)
        available_sites.remove(best_site)
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
