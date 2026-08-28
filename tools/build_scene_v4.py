#!/usr/bin/env python3
"""Validate V4 terminal sources and atomically synchronize 1C templates."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_TEMPLATES_DIR = ROOT / "src" / "DataProcessors" / "d3_v4" / "Templates"
AXIS_VERTEX_TOLERANCE_M = 0.15
GEOMETRY_TOLERANCE_M = 0.05
RAIL_OVERLAY_TOLERANCE_M = 0.5
RAIL_OVERLAY_SAMPLE_STEP_M = 1.0
ANCHORAGE_SAMPLE_STEP_M = 1.0
PLANNED_BERTHING_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
VESSEL_TYPES = frozenset(("container", "carCarrier", "coal", "fish", "general", "bunker", "empty"))
COORDINATE_PRECISION = 2
RESIZE_TOLERANCE_M = math.sqrt(2) * 0.5 * 10**-COORDINATE_PRECISION + 1e-9

SOURCE_FILES = {
    "terminalLayout": "terminal-layout-v4.json",
    "sceneOrigin": "scene_origin.txt",
    "shoreline": "geojson/shoreline.geojson",
    "water": "geojson/water.geojson",
    "buildings": "geojson/buildings.geojson",
    "roads": "geojson/roads.geojson",
    "portBoundary": "geojson/port_boundary.geojson",
    "berth": "geojson/berth.geojson",
    "vessels": "vessels-v4.json",
    "portalCranes": "geojson/portal_cranes_v4.geojson",
    "yardCranes": "yard-cranes-v4.json",
    "containerSites": "geojson/container_sites_v4.geojson",
    "bayAxes": "geojson/container_site_bay_axes_v4.geojson",
    "railwaysVisual": "geojson/railways_visual_v4.geojson",
    "railwayBranches": "geojson/railway_branches_v4.geojson",
    "trains": "trains-v4.json",
    "anchorage": "geojson/anchorage.geojson",
    "anchorageVessels": "anchorage-vessels.json",
}

TEMPLATE_PATHS = {
    "terminalLayout": "TerminalLayoutV4_json/Template.txt",
    "sceneOrigin": "SceneOrigin_txt/Template.txt",
    "shoreline": "Shoreline_geojson/Template.txt",
    "water": "Water_geojson/Template.txt",
    "buildings": "Buildings_geojson/Template.txt",
    "roads": "Roads_geojson/Template.txt",
    "portBoundary": "PortBoundary_geojson/Template.txt",
    "berths": "BerthsV4_json/Template.txt",
    "vessels": "VesselsV4_json/Template.txt",
    "cranes": "CranesV4_json/Template.txt",
    "containerSites": "ContainerSitesV4_geojson/Template.txt",
    "bayAxes": "ContainerSiteBayAxesV4_geojson/Template.txt",
    "railways": "RailwaysV4_json/Template.txt",
    "anchorage": "Anchorage_geojson/Template.txt",
    "anchorageVessels": "AnchorageVessels_json/Template.txt",
    "manifest": "SceneManifestV4_json/Template.txt",
}

DEFAULT_RAIL_GAUGE_M = 1.52
DEFAULT_RAIL_COLOR = "#6B7280"
DEFAULT_TRAIN_GAP_M = 1.0
WAGON_DEFAULTS = {
    "covered": {"lengthM": 15.7, "widthM": 3.25, "heightM": 4.7, "color": "#8B5A2B"},
    "gondola": {"lengthM": 13.92, "widthM": 3.24, "heightM": 3.48, "color": "#4B5563"},
    "refrigerated": {"lengthM": 21.0, "widthM": 3.1, "heightM": 4.7, "color": "#DCEAF7"},
    "carCarrier": {"lengthM": 24.0, "widthM": 3.1, "heightM": 4.8, "color": "#94A3B8"},
    "thermos": {"lengthM": 21.0, "widthM": 3.1, "heightM": 4.7, "color": "#E5E7EB"},
    "fittingPlatform": {
        "40": {"lengthM": 13.4, "widthM": 3.1, "heightM": 1.3, "color": "#B45309"},
        "60": {"lengthM": 19.62, "widthM": 3.1, "heightM": 1.3, "color": "#B45309"},
        "80": {"lengthM": 25.8, "widthM": 3.1, "heightM": 1.3, "color": "#B45309"},
    },
}
WAGON_TYPES = frozenset(WAGON_DEFAULTS)
LOAD_STATUSES = frozenset(("empty", "loaded"))
PLATFORM_LENGTHS_FT = frozenset((40, 60, 80))
CONTAINER_LENGTHS_FT = frozenset((20, 40))
CONTAINER_CAPACITY_TEU = {"gondola": 2, 40: 2, 60: 3, 80: 4}
CARGO_KINDS = {
    "covered": frozenset(("general",)),
    "gondola": frozenset(("coal", "containers")),
    "refrigerated": frozenset(("refrigerated",)),
    "carCarrier": frozenset(("automobiles",)),
    "thermos": frozenset(("temperatureControlled",)),
    "fittingPlatform": frozenset(("containers",)),
}


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def add(self, location: str, message: str) -> None:
        self.errors.append(f"{location}: {message}")

    def require(self, condition: bool, location: str, message: str) -> bool:
        if not condition:
            self.add(location, message)
        return condition

    def require_ids(
        self,
        actual: set[str],
        expected: set[str],
        location: str,
        message: str,
        *,
        actual_label: str,
        expected_label: str,
    ) -> bool:
        if actual == expected:
            return True
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing from {actual_label} ({len(missing)}): {', '.join(missing)}")
        if extra:
            details.append(f"only in {actual_label}, not in {expected_label} ({len(extra)}): {', '.join(extra)}")
        self.add(location, f"{message}; {'; '.join(details)}")
        return False

    def finish(self) -> None:
        if not self.errors:
            return
        lines = [f"V4 scene validation failed ({len(self.errors)} error(s)):"]
        lines.extend(f"  - {error}" for error in self.errors)
        raise SystemExit("\n".join(lines))


def read_text(path: Path, validation: Validation) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        validation.add(str(path), f"cannot read file: {error}")
        return ""
    if not text.strip():
        validation.add(str(path), "file is empty")
    return text


def read_json(path: Path, text: str, validation: Validation) -> Any:
    if not text.strip():
        return {}
    try:
        return json.loads(text, parse_constant=lambda value: _reject_constant(value))
    except (json.JSONDecodeError, ValueError) as error:
        validation.add(str(path), f"invalid JSON: {error}")
        return {}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    if not number(value[0]) or not number(value[1]):
        return None
    return float(value[0]), float(value[1])


def distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def point_to_segment_distance(
    sample: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-24:
        return distance(sample, start)
    fraction = ((sample[0] - start[0]) * dx + (sample[1] - start[1]) * dy) / length_sq
    fraction = min(max(fraction, 0.0), 1.0)
    closest = (start[0] + fraction * dx, start[1] + fraction * dy)
    return distance(sample, closest)


def polyline_segments(
    coordinates: list[list[float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for index in range(1, len(coordinates)):
        start = (float(coordinates[index - 1][0]), float(coordinates[index - 1][1]))
        end = (float(coordinates[index][0]), float(coordinates[index][1]))
        if distance(start, end) > 1e-12:
            segments.append((start, end))
    return segments


def sample_polyline(
    coordinates: list[list[float]],
    step_m: float,
) -> list[tuple[float, float]]:
    if not coordinates:
        return []
    points = [(float(coordinate[0]), float(coordinate[1])) for coordinate in coordinates]
    samples = [points[0]]
    chainage = 0.0
    next_sample = step_m
    for index in range(1, len(points)):
        start = points[index - 1]
        end = points[index]
        segment_length = distance(start, end)
        if segment_length <= 1e-12:
            continue
        while next_sample < chainage + segment_length - 1e-12:
            fraction = (next_sample - chainage) / segment_length
            samples.append(
                (
                    start[0] + (end[0] - start[0]) * fraction,
                    start[1] + (end[1] - start[1]) * fraction,
                )
            )
            next_sample += step_m
        chainage += segment_length
        samples.append(end)
    return samples


def validate_branch_overlay(
    branches: list[dict[str, Any]],
    visual_paths: list[dict[str, Any]],
    validation: Validation,
) -> None:
    visual_segments = [
        segment
        for path in visual_paths
        for segment in polyline_segments(path["coordinates"])
    ]
    for branch in branches:
        max_offset = max(
            (
                min(
                    point_to_segment_distance(sample, start, end)
                    for start, end in visual_segments
                )
                if visual_segments
                else math.inf
            )
            for sample in sample_polyline(
                branch["coordinates"],
                RAIL_OVERLAY_SAMPLE_STEP_M,
            )
        )
        if max_offset > RAIL_OVERLAY_TOLERANCE_M:
            validation.add(
                f"railway_branches_v4.geojson branch {branch['id']}",
                "must lie on railways_visual_v4 within "
                f"{RAIL_OVERLAY_TOLERANCE_M:g} m (maximum offset {max_offset:.2f} m)",
            )


def crs_code(document: Any) -> str:
    try:
        raw = str(document["crs"]["properties"]["name"])
    except (KeyError, TypeError):
        return ""
    match = re.search(r"EPSG(?::|::)(\d+)$", raw, re.IGNORECASE)
    return f"EPSG:{match.group(1)}" if match else raw


def parse_scene_origin(text: str, validation: Validation) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            validation.add(f"scene_origin.txt:{line_number}", "expected key=value")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in result:
            validation.add(f"scene_origin.txt:{line_number}", f"duplicate key {key}")
        result[key] = value
    for key in ("originEasting", "originNorthing", "rotationDeg"):
        try:
            result[key] = float(result[key])
            if not math.isfinite(result[key]):
                raise ValueError
        except (KeyError, ValueError):
            validation.add("scene_origin.txt", f"{key} must be a finite number")
    validation.require(bool(result.get("crs")), "scene_origin.txt", "crs is required")
    return result


def feature_collection(document: Any, location: str, validation: Validation) -> list[Any]:
    if not validation.require(
        isinstance(document, dict) and document.get("type") == "FeatureCollection",
        location,
        "expected GeoJSON FeatureCollection",
    ):
        return []
    features = document.get("features")
    if not validation.require(isinstance(features, list), location, "features must be an array"):
        return []
    return features


def polygon_vertices(feature: Any, location: str, validation: Validation) -> list[tuple[float, float]]:
    try:
        geometry = feature["geometry"]
        coordinates = geometry["coordinates"]
    except (KeyError, TypeError):
        validation.add(location, "missing geometry")
        return []
    if geometry.get("type") != "Polygon":
        validation.add(location, "geometry must be Polygon")
        return []
    if not isinstance(coordinates, list) or not coordinates or not isinstance(coordinates[0], list):
        validation.add(location, "Polygon must contain an exterior ring")
        return []
    vertices: list[tuple[float, float]] = []
    for index, raw_point in enumerate(coordinates[0]):
        parsed = point(raw_point)
        if parsed is None:
            validation.add(f"{location}.geometry.coordinates[0][{index}]", "invalid coordinate")
        else:
            vertices.append(parsed)
    if len(vertices) >= 2 and distance(vertices[0], vertices[-1]) <= GEOMETRY_TOLERANCE_M:
        vertices.pop()
    validation.require(len(vertices) == 4, location, "site polygon must have exactly four vertices")
    if len(vertices) == 4:
        vectors = [
            (
                vertices[(index + 1) % 4][0] - vertices[index][0],
                vertices[(index + 1) % 4][1] - vertices[index][1],
            )
            for index in range(4)
        ]
        lengths = [math.hypot(vector[0], vector[1]) for vector in vectors]
        for index, length in enumerate(lengths):
            validation.require(length > GEOMETRY_TOLERANCE_M, location, f"side {index + 1} is empty")
        for index in range(4):
            left = vectors[index]
            right = vectors[(index + 1) % 4]
            denominator = max(lengths[index] * lengths[(index + 1) % 4], 1e-9)
            validation.require(
                abs(left[0] * right[0] + left[1] * right[1]) / denominator <= 0.01,
                location,
                "site polygon must be rectangular",
            )
    return vertices


def validate_water(document: Any, crs: str, validation: Validation) -> None:
    features = feature_collection(document, "water.geojson", validation)
    validation.require(bool(features), "water.geojson", "at least one water polygon is required")
    for feature_index, feature in enumerate(features):
        location = f"water.geojson.features[{feature_index}]"
        try:
            geometry = feature["geometry"]
            geometry_type = geometry["type"]
            coordinates = geometry["coordinates"]
        except (KeyError, TypeError):
            validation.add(location, "missing geometry")
            continue
        if geometry_type not in ("Polygon", "MultiPolygon"):
            validation.add(location, "geometry must be Polygon or MultiPolygon")
            continue
        polygons = coordinates if geometry_type == "MultiPolygon" else [coordinates]
        for polygon_index, polygon in enumerate(polygons):
            if not isinstance(polygon, list) or not polygon:
                validation.add(location, f"polygon {polygon_index} has no rings")
                continue
            for ring_index, ring in enumerate(polygon):
                ring_location = f"{location}.polygon[{polygon_index}].ring[{ring_index}]"
                parsed = [point(value) for value in ring] if isinstance(ring, list) else []
                validation.require(
                    len(parsed) >= 4 and all(value is not None for value in parsed),
                    ring_location,
                    "ring must contain at least four valid coordinates",
                )
                if len(parsed) >= 2 and parsed[0] is not None and parsed[-1] is not None:
                    validation.require(
                        distance(parsed[0], parsed[-1]) <= GEOMETRY_TOLERANCE_M,
                        ring_location,
                        "ring must be closed",
                    )
    validation.require(crs_code(document) == crs, "water.geojson", f"CRS must be {crs}")


def validate_buildings(document: Any, crs: str, validation: Validation) -> None:
    building_ids: set[str] = set()
    for feature_index, feature in enumerate(feature_collection(document, "buildings.geojson", validation)):
        location = f"buildings.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            validation.add(location, "properties object is required")
            properties = {}

        raw_building_id = properties.get("id")
        raw_name = properties.get("name")
        building_id = raw_building_id.strip() if isinstance(raw_building_id, str) else ""
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        color = properties.get("color")
        if not validation.require(bool(building_id), f"{location}.properties.id", "id is required"):
            building_id = ""
        elif building_id in building_ids:
            validation.add(f"{location}.properties.id", f"duplicate id {building_id}")
        else:
            building_ids.add(building_id)
        validation.require(bool(name), f"{location}.properties.name", "name is required")
        validation.require(
            isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color) is not None,
            f"{location}.properties.color",
            "color must use #RRGGBB format",
        )

        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
            validation.add(f"{location}.geometry", "geometry must be Polygon")
            continue
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) != 1:
            validation.add(f"{location}.geometry.coordinates", "Polygon must contain one exterior ring without holes")
            continue
        ring = coordinates[0]
        parsed = [point(value) for value in ring] if isinstance(ring, list) else []
        validation.require(
            len(parsed) >= 4 and all(value is not None for value in parsed),
            f"{location}.geometry.coordinates[0]",
            "ring must contain at least four valid coordinates",
        )
        if len(parsed) >= 2 and parsed[0] is not None and parsed[-1] is not None:
            validation.require(
                distance(parsed[0], parsed[-1]) <= GEOMETRY_TOLERANCE_M,
                f"{location}.geometry.coordinates[0]",
                "ring must be closed",
            )
        valid_points = [value for value in parsed if value is not None]
        if len(valid_points) >= 4:
            vertices = valid_points[:-1]
            doubled_area = sum(
                vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
                - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
                for index in range(len(vertices))
            )
            validation.require(
                len(set(vertices)) >= 3 and abs(doubled_area) > GEOMETRY_TOLERANCE_M**2,
                f"{location}.geometry.coordinates[0]",
                "ring must have a non-zero area",
            )
    validation.require(crs_code(document) == crs, "buildings.geojson", f"CRS must be {crs}")


def validate_roads(document: Any, crs: str, validation: Validation) -> None:
    road_ids: set[str] = set()
    for feature_index, feature in enumerate(feature_collection(document, "roads.geojson", validation)):
        location = f"roads.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            validation.add(location, "properties object is required")
            properties = {}

        road_id = trimmed_text(properties.get("id"))
        color = properties.get("color")
        width = properties.get("width_m")
        if not validation.require(bool(road_id), f"{location}.properties.id", "id is required"):
            road_id = ""
        elif road_id in road_ids:
            validation.add(f"{location}.properties.id", f"duplicate id {road_id}")
        else:
            road_ids.add(road_id)
        validation.require(
            valid_color(color),
            f"{location}.properties.color",
            "color must use #RRGGBB format",
        )
        validation.require(
            number(width) and float(width) > 0,
            f"{location}.properties.width_m",
            "width_m must be a positive finite number",
        )

        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            validation.add(f"{location}.geometry", "geometry must be LineString")
            continue
        raw_coordinates = geometry.get("coordinates")
        parsed = [point(value) for value in raw_coordinates] if isinstance(raw_coordinates, list) else []
        if not validation.require(
            len(parsed) >= 2 and all(value is not None for value in parsed),
            f"{location}.geometry.coordinates",
            "LineString must contain at least two finite points",
        ):
            continue
        coordinates = [value for value in parsed if value is not None]
        path_length = sum(
            distance(coordinates[index - 1], coordinates[index]) for index in range(1, len(coordinates))
        )
        validation.require(
            path_length > GEOMETRY_TOLERANCE_M,
            f"{location}.geometry.coordinates",
            "LineString must contain at least two different points",
        )
    validation.require(crs_code(document) == crs, "roads.geojson", f"CRS must be {crs}")


def validate_port_boundary(document: Any, crs: str, validation: Validation) -> None:
    features = feature_collection(document, "port_boundary.geojson", validation)
    validation.require(
        len(features) == 1,
        "port_boundary.geojson",
        "exactly one LineString feature is required",
    )
    for feature_index, feature in enumerate(features):
        location = f"port_boundary.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            validation.add(location, "properties object is required")
            properties = {}

        boundary_id = trimmed_text(properties.get("id"))
        color = properties.get("color")
        width = properties.get("width_m")
        if not validation.require(bool(boundary_id), f"{location}.properties.id", "id is required"):
            boundary_id = ""
        validation.require(
            valid_color(color),
            f"{location}.properties.color",
            "color must use #RRGGBB format",
        )
        validation.require(
            number(width) and float(width) > 0,
            f"{location}.properties.width_m",
            "width_m must be a positive finite number",
        )

        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            validation.add(f"{location}.geometry", "geometry must be LineString")
            continue
        raw_coordinates = geometry.get("coordinates")
        parsed = [point(value) for value in raw_coordinates] if isinstance(raw_coordinates, list) else []
        if not validation.require(
            len(parsed) >= 2 and all(value is not None for value in parsed),
            f"{location}.geometry.coordinates",
            "LineString must contain at least two finite points",
        ):
            continue
        coordinates = [value for value in parsed if value is not None]
        path_length = sum(
            distance(coordinates[index - 1], coordinates[index]) for index in range(1, len(coordinates))
        )
        validation.require(
            path_length > GEOMETRY_TOLERANCE_M,
            f"{location}.geometry.coordinates",
            "LineString must contain at least two different points",
        )
    validation.require(crs_code(document) == crs, "port_boundary.geojson", f"CRS must be {crs}")


def validate_shoreline(document: Any, crs: str, validation: Validation) -> None:
    features = feature_collection(document, "shoreline.geojson", validation)
    point_count = 0
    for feature_index, feature in enumerate(features):
        location = f"shoreline.geojson.features[{feature_index}]"
        try:
            geometry = feature["geometry"]
            coordinates = geometry["coordinates"]
        except (KeyError, TypeError):
            validation.add(location, "missing geometry")
            continue
        if geometry.get("type") != "LineString":
            validation.add(location, "geometry must be LineString")
            continue
        valid_points = [point(value) for value in coordinates] if isinstance(coordinates, list) else []
        validation.require(
            len(valid_points) >= 2 and all(value is not None for value in valid_points),
            location,
            "LineString must contain at least two valid coordinates",
        )
        point_count += len(valid_points)
    validation.require(point_count >= 2, "shoreline.geojson", "shoreline is empty")
    validation.require(crs_code(document) == crs, "shoreline.geojson", f"CRS must be {crs}")


def collect_sites(document: Any, crs: str, validation: Validation) -> dict[str, dict[str, Any]]:
    sites: dict[str, dict[str, Any]] = {}
    for feature_index, feature in enumerate(feature_collection(document, "container_sites_v4.geojson", validation)):
        location = f"container_sites_v4.geojson.features[{feature_index}]"
        try:
            site_id = str(feature["properties"]["site_id"]).strip()
        except (KeyError, TypeError):
            site_id = ""
        if not validation.require(bool(site_id), location, "site_id is required"):
            continue
        if site_id in sites:
            validation.add(location, f"duplicate site_id {site_id}")
            continue
        vertices = polygon_vertices(feature, location, validation)
        sites[site_id] = {"feature": feature, "vertices": vertices}
    validation.require(crs_code(document) == crs, "container_sites_v4.geojson", f"CRS must be {crs}")
    return sites


def collect_axes(document: Any, crs: str, validation: Validation) -> dict[str, dict[str, Any]]:
    axes: dict[str, dict[str, Any]] = {}
    for feature_index, feature in enumerate(
        feature_collection(document, "container_site_bay_axes_v4.geojson", validation)
    ):
        location = f"container_site_bay_axes_v4.geojson.features[{feature_index}]"
        try:
            site_id = str(feature["properties"]["site_id"]).strip()
            geometry = feature["geometry"]
            coordinates = geometry["coordinates"]
        except (KeyError, TypeError):
            validation.add(location, "site_id and geometry are required")
            continue
        if not validation.require(bool(site_id), location, "site_id is required"):
            continue
        if site_id in axes:
            validation.add(location, f"duplicate site_id {site_id}")
            continue
        if geometry.get("type") != "LineString" or not isinstance(coordinates, list) or len(coordinates) != 2:
            validation.add(location, "geometry must be a two-point LineString")
            continue
        parsed = [point(value) for value in coordinates]
        if not all(value is not None for value in parsed):
            validation.add(location, "axis coordinates must be finite numbers")
            continue
        axis = [value for value in parsed if value is not None]
        validation.require(distance(axis[0], axis[1]) >= 0.5, location, "axis is too short")
        axes[site_id] = {"feature": feature, "points": axis}
    validation.require(crs_code(document) == crs, "container_site_bay_axes_v4.geojson", f"CRS must be {crs}")
    return axes


def collect_berths(
    axes_document: Any,
    crs: str,
    validation: Validation,
) -> dict[str, Any]:
    berths: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for feature_index, feature in enumerate(
        feature_collection(axes_document, "berth.geojson", validation)
    ):
        location = f"berth.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        berth_id = properties.get("berth_id") if isinstance(properties, dict) else None
        name = properties.get("name") if isinstance(properties, dict) else None
        berth_id = berth_id.strip() if isinstance(berth_id, str) else ""
        name = name.strip() if isinstance(name, str) else ""
        validation.require(bool(berth_id), f"{location}.properties.berth_id", "berth_id is required")
        validation.require(bool(name), f"{location}.properties.name", "name is required")
        if berth_id in seen_ids:
            validation.add(f"{location}.properties.berth_id", f"duplicate berth_id {berth_id}")
            continue
        if (
            not isinstance(geometry, dict)
            or geometry.get("type") != "LineString"
            or not isinstance(geometry.get("coordinates"), list)
            or len(geometry["coordinates"]) != 2
        ):
            validation.add(f"{location}.geometry", "geometry must be a two-point LineString")
            continue
        parsed = [point(value) for value in geometry["coordinates"]]
        if not all(value is not None for value in parsed):
            validation.add(f"{location}.geometry.coordinates", "coordinates must be finite points")
            continue
        axis = [value for value in parsed if value is not None]
        validation.require(distance(axis[0], axis[1]) >= 0.5, location, "heading axis is too short")
        if not berth_id:
            continue
        seen_ids.add(berth_id)
        berths.append(
            {
                "id": berth_id,
                "name": name,
                "center": list(axis[0]),
                "headingPoint": list(axis[1]),
            }
        )
    validation.require(crs_code(axes_document) == crs, "berth.geojson", f"CRS must be {crs}")
    return {"schemaVersion": "4.0", "crs": crs, "berths": berths}


def validate_vessels(document: Any, berth_ids: set[str], validation: Validation) -> None:
    if not isinstance(document, dict):
        validation.add("vessels-v4.json", "root must be an object")
        return
    validation.require(document.get("schemaVersion") == "4.0", "vessels-v4.json", "schemaVersion must be 4.0")
    vessels = document.get("vessels")
    if not isinstance(vessels, list):
        validation.add("vessels-v4.json.vessels", "vessels must be an array")
        return
    vessel_ids: set[str] = set()
    occupied_berths: set[str] = set()
    for vessel_index, vessel in enumerate(vessels):
        location = f"vessels-v4.json.vessels[{vessel_index}]"
        if not isinstance(vessel, dict):
            validation.add(location, "vessel must be an object")
            continue
        vessel_id = vessel.get("id")
        name = vessel.get("name")
        berth_id = vessel.get("berthId")
        vessel_id = vessel_id.strip() if isinstance(vessel_id, str) else ""
        name = name.strip() if isinstance(name, str) else ""
        berth_id = berth_id.strip() if isinstance(berth_id, str) else ""
        validation.require(bool(vessel_id), f"{location}.id", "id is required")
        validation.require(bool(name), f"{location}.name", "name is required")
        if vessel_id in vessel_ids:
            validation.add(f"{location}.id", f"duplicate vessel id {vessel_id}")
        elif vessel_id:
            vessel_ids.add(vessel_id)
        validation.require(number(vessel.get("lengthM")) and vessel["lengthM"] > 0, f"{location}.lengthM", "must be positive")
        validation.require(number(vessel.get("beamM")) and vessel["beamM"] > 0, f"{location}.beamM", "must be positive")
        validation.require(berth_id in berth_ids, f"{location}.berthId", f"unknown berthId {berth_id!r}")
        if berth_id in occupied_berths:
            validation.add(f"{location}.berthId", f"berth {berth_id} already has a vessel")
        elif berth_id:
            occupied_berths.add(berth_id)
        validation.require(
            vessel.get("mooringSide") in ("right", "left", "bow", "stern"),
            f"{location}.mooringSide",
            "must be right, left, bow or stern",
        )
        validation.require(
            vessel.get("vesselType") in VESSEL_TYPES,
            f"{location}.vesselType",
            "must be container, carCarrier, coal, fish, general, bunker or empty",
        )
        color = vessel.get("color")
        if color is not None:
            validation.require(
                isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color) is not None,
                f"{location}.color",
                "color must use #RRGGBB format",
            )
        clearance = vessel.get("clearanceM")
        if clearance is not None:
            validation.require(number(clearance) and clearance >= 0, f"{location}.clearanceM", "must be non-negative")
        refueling = vessel.get("refueling")
        if refueling is not None:
            validation.require(isinstance(refueling, bool), f"{location}.refueling", "must be boolean")


def water_rings(document: Any) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    if not isinstance(document, dict) or not isinstance(document.get("features"), list):
        return rings
    for feature in document["features"]:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict):
            continue
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        polygons = coordinates if geometry_type == "MultiPolygon" else [coordinates] if geometry_type == "Polygon" else []
        if not isinstance(polygons, list):
            continue
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon or not isinstance(polygon[0], list):
                continue
            parsed = [point(value) for value in polygon[0]]
            if len(parsed) >= 4 and all(value is not None for value in parsed):
                rings.append([value for value in parsed if value is not None])
    return rings


def point_in_ring(sample: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    vertices = ring[:-1] if len(ring) >= 2 and distance(ring[0], ring[-1]) <= GEOMETRY_TOLERANCE_M else ring
    if len(vertices) < 3:
        return False
    x, y = sample
    inside = False
    previous_index = len(vertices) - 1
    for index, (xi, yi) in enumerate(vertices):
        xj, yj = vertices[previous_index]
        if (yi > y) != (yj > y) and abs(yj - yi) > 1e-18:
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        previous_index = index
    return inside


def point_in_water(sample: tuple[float, float], rings: list[list[tuple[float, float]]]) -> bool:
    return any(point_in_ring(sample, ring) for ring in rings)


def valid_planned_berthing(value: Any) -> bool:
    if not isinstance(value, str) or PLANNED_BERTHING_PATTERN.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def non_negative_int(value: Any) -> bool:
    return number(value) and float(value) >= 0 and float(value) == int(value)


PLANNED_CONTAINER_KEYS = ("ft10", "ft20", "ft40")


def valid_planned_containers(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(PLANNED_CONTAINER_KEYS):
        return False
    return all(non_negative_int(value[key]) for key in PLANNED_CONTAINER_KEYS)


def validate_anchorage(document: Any, water_document: Any, crs: str, validation: Validation) -> None:
    features = feature_collection(document, "anchorage.geojson", validation)
    validation.require(
        len(features) == 1,
        "anchorage.geojson",
        "exactly one LineString feature is required",
    )
    rings = water_rings(water_document)
    for feature_index, feature in enumerate(features):
        location = f"anchorage.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            validation.add(location, "properties object is required")
            properties = {}

        fid = properties.get("fid")
        gap = properties.get("gap_m")
        validation.require(
            fid is not None and fid != "",
            f"{location}.properties.fid",
            "fid is required",
        )
        validation.require(
            number(gap) and float(gap) > 0,
            f"{location}.properties.gap_m",
            "gap_m must be a positive finite number",
        )

        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if (
            not isinstance(geometry, dict)
            or geometry.get("type") != "LineString"
            or not isinstance(geometry.get("coordinates"), list)
            or len(geometry["coordinates"]) != 2
        ):
            validation.add(f"{location}.geometry", "geometry must be a two-point LineString")
            continue
        parsed = [point(value) for value in geometry["coordinates"]]
        if not all(value is not None for value in parsed):
            validation.add(f"{location}.geometry.coordinates", "coordinates must be finite points")
            continue
        axis = [value for value in parsed if value is not None]
        validation.require(distance(axis[0], axis[1]) >= 0.5, location, "heading axis is too short")
        if not rings:
            validation.add("water.geojson", "water polygon is required to place the anchorage axis")
            continue
        if any(
            not point_in_water(sample, rings)
            for sample in sample_polyline(geometry["coordinates"], ANCHORAGE_SAMPLE_STEP_M)
        ):
            validation.add(location, "axis must lie entirely inside water.geojson")
    validation.require(crs_code(document) == crs, "anchorage.geojson", f"CRS must be {crs}")


def validate_anchorage_vessels(
    document: Any,
    berth_vessel_ids: set[str],
    berth_ids: set[str],
    validation: Validation,
) -> None:
    if not isinstance(document, dict):
        validation.add("anchorage-vessels.json", "root must be an object")
        return
    validation.require(
        document.get("schemaVersion") == "4.0",
        "anchorage-vessels.json",
        "schemaVersion must be 4.0",
    )
    vessels = document.get("vessels")
    if not isinstance(vessels, list):
        validation.add("anchorage-vessels.json.vessels", "vessels must be an array")
        return
    vessel_ids: set[str] = set()
    for vessel_index, vessel in enumerate(vessels):
        location = f"anchorage-vessels.json.vessels[{vessel_index}]"
        if not isinstance(vessel, dict):
            validation.add(location, "vessel must be an object")
            continue
        vessel_id = trimmed_text(vessel.get("id"))
        name = trimmed_text(vessel.get("name"))
        validation.require(bool(vessel_id), f"{location}.id", "id is required")
        validation.require(bool(name), f"{location}.name", "name is required")
        if vessel_id in vessel_ids:
            validation.add(f"{location}.id", f"duplicate vessel id {vessel_id}")
        elif vessel_id in berth_vessel_ids:
            validation.add(f"{location}.id", f"vessel id {vessel_id} already used in vessels-v4.json")
        elif vessel_id:
            vessel_ids.add(vessel_id)
        validation.require(
            number(vessel.get("lengthM")) and vessel["lengthM"] > 0,
            f"{location}.lengthM",
            "must be positive",
        )
        validation.require(
            number(vessel.get("beamM")) and vessel["beamM"] > 0,
            f"{location}.beamM",
            "must be positive",
        )
        validation.require(
            valid_planned_berthing(vessel.get("plannedBerthingAt")),
            f"{location}.plannedBerthingAt",
            "must be YYYY-MM-DDTHH:MM:SS",
        )
        planned_berth_id = trimmed_text(vessel.get("plannedBerthId"))
        validation.require(bool(planned_berth_id), f"{location}.plannedBerthId", "plannedBerthId is required")
        validation.require(
            planned_berth_id in berth_ids,
            f"{location}.plannedBerthId",
            f"unknown plannedBerthId {planned_berth_id!r}",
        )
        validation.require(
            valid_planned_containers(vessel.get("plannedLoad")),
            f"{location}.plannedLoad",
            "must be an object with non-negative integer ft10, ft20 and ft40",
        )
        validation.require(
            valid_planned_containers(vessel.get("plannedUnload")),
            f"{location}.plannedUnload",
            "must be an object with non-negative integer ft10, ft20 and ft40",
        )
        validation.require(
            vessel.get("vesselType") in VESSEL_TYPES,
            f"{location}.vesselType",
            "must be container, carCarrier, coal, fish, general, bunker or empty",
        )
        color = vessel.get("color")
        if color is not None:
            validation.require(
                isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color) is not None,
                f"{location}.color",
                "color must use #RRGGBB format",
            )


def valid_color(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None


def trimmed_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_1c_property_names(value: Any, location: str, validation: Validation) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            validation.require(
                isinstance(key, str) and re.fullmatch(r"(?:[^\W\d]|_)\w*", key, re.UNICODE) is not None,
                f"{location}.{key}",
                "property name is not compatible with 1C Structure",
            )
            validate_1c_property_names(nested, f"{location}.{key}", validation)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            validate_1c_property_names(nested, f"{location}[{index}]", validation)


def reject_unknown_keys(
    value: dict[str, Any],
    allowed: set[str] | frozenset[str],
    location: str,
    validation: Validation,
) -> None:
    for key in sorted(set(value) - set(allowed)):
        validation.add(f"{location}.{key}", "unsupported property")


def collect_line_paths(
    document: Any,
    source_name: str,
    crs: str,
    branches: bool,
    validation: Validation,
) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    branch_ids: set[str] = set()
    for feature_index, feature in enumerate(feature_collection(document, source_name, validation)):
        location = f"{source_name}.features[{feature_index}]"
        if not isinstance(feature, dict):
            validation.add(location, "feature must be an object")
            continue
        properties = feature.get("properties")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            validation.add(f"{location}.properties", "properties must be an object")
            properties = {}
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            validation.add(f"{location}.geometry", "geometry must be LineString")
            continue
        raw_coordinates = geometry.get("coordinates")
        parsed = [point(value) for value in raw_coordinates] if isinstance(raw_coordinates, list) else []
        if not validation.require(
            len(parsed) >= 2 and all(value is not None for value in parsed),
            f"{location}.geometry.coordinates",
            "LineString must contain at least two finite points",
        ):
            continue
        coordinates = [value for value in parsed if value is not None]
        path_length = sum(distance(coordinates[index - 1], coordinates[index]) for index in range(1, len(coordinates)))
        if not validation.require(
            path_length > GEOMETRY_TOLERANCE_M,
            f"{location}.geometry.coordinates",
            "LineString must contain at least two different points",
        ):
            continue

        gauge = properties.get("gauge_m")
        if gauge is None:
            gauge = DEFAULT_RAIL_GAUGE_M
        color = properties.get("color")
        if color is None:
            color = DEFAULT_RAIL_COLOR
        validation.require(
            number(gauge) and float(gauge) > 0,
            f"{location}.properties.gauge_m",
            "gauge_m must be a positive finite number",
        )
        validation.require(
            valid_color(color),
            f"{location}.properties.color",
            "color must use #RRGGBB format",
        )
        normalized: dict[str, Any] = {
            "gaugeM": float(gauge) if number(gauge) and float(gauge) > 0 else DEFAULT_RAIL_GAUGE_M,
            "color": color.upper() if valid_color(color) else DEFAULT_RAIL_COLOR,
            "lengthM": round(path_length, 3),
            "coordinates": [[coordinate[0], coordinate[1]] for coordinate in coordinates],
        }
        if branches:
            branch_id = trimmed_text(properties.get("branch_id"))
            name = trimmed_text(properties.get("name"))
            validation.require(bool(branch_id), f"{location}.properties.branch_id", "branch_id is required")
            validation.require(bool(name), f"{location}.properties.name", "name is required")
            if branch_id in branch_ids:
                validation.add(f"{location}.properties.branch_id", f"duplicate branch_id {branch_id}")
            elif branch_id:
                branch_ids.add(branch_id)
            if not branch_id:
                continue
            normalized = {"id": branch_id, "name": name, **normalized}
        else:
            path_id = trimmed_text(properties.get("id"))
            name = trimmed_text(properties.get("name"))
            if path_id:
                normalized = {"id": path_id, **normalized}
            if name:
                normalized = {**({"id": normalized["id"]} if "id" in normalized else {}), "name": name, **{
                    key: value for key, value in normalized.items() if key != "id"
                }}
        paths.append(normalized)
    validation.require(crs_code(document) == crs, source_name, f"CRS must be {crs}")
    return paths


def interpolate_path(
    coordinates: list[list[float]],
    chainage: float,
) -> tuple[list[float], list[float]]:
    remaining = chainage
    fallback_tangent = [1.0, 0.0]
    for index in range(1, len(coordinates)):
        first = coordinates[index - 1]
        second = coordinates[index]
        segment_length = math.hypot(second[0] - first[0], second[1] - first[1])
        if segment_length <= 1e-12:
            continue
        tangent = [(second[0] - first[0]) / segment_length, (second[1] - first[1]) / segment_length]
        fallback_tangent = tangent
        if remaining <= segment_length or index == len(coordinates) - 1:
            fraction = min(max(remaining / segment_length, 0.0), 1.0)
            return (
                [
                    round(first[0] + (second[0] - first[0]) * fraction, 3),
                    round(first[1] + (second[1] - first[1]) * fraction, 3),
                ],
                [round(tangent[0], 6), round(tangent[1], 6)],
            )
        remaining -= segment_length
    return [round(value, 3) for value in coordinates[-1]], fallback_tangent


def wagon_defaults(wagon_type: str, platform_length_ft: Any) -> dict[str, Any]:
    defaults = WAGON_DEFAULTS[wagon_type]
    if wagon_type == "fittingPlatform":
        return defaults[str(platform_length_ft)]  # type: ignore[index]
    return defaults  # type: ignore[return-value]


def normalize_cargo(
    cargo: Any,
    wagon_type: str,
    platform_length_ft: Any,
    location: str,
    container_ids: set[str],
    validation: Validation,
) -> dict[str, Any] | None:
    if not isinstance(cargo, dict):
        validation.add(location, "loaded wagon cargo must be an object")
        return None
    kind = cargo.get("kind")
    if kind not in CARGO_KINDS[wagon_type]:
        validation.add(
            f"{location}.kind",
            f"unsupported cargo kind {kind!r} for wagon type {wagon_type}",
        )
        return None
    if kind == "general":
        reject_unknown_keys(cargo, {"kind", "description"}, location, validation)
        description = trimmed_text(cargo.get("description"))
        validation.require(bool(description), f"{location}.description", "description is required")
        return {"kind": kind, "description": description}
    if kind != "containers":
        reject_unknown_keys(cargo, {"kind"}, location, validation)
        return {"kind": kind}

    reject_unknown_keys(cargo, {"kind", "containers"}, location, validation)
    raw_containers = cargo.get("containers")
    if not isinstance(raw_containers, list) or not raw_containers:
        validation.add(f"{location}.containers", "loaded container cargo requires a non-empty array")
        return {"kind": kind, "containers": []}
    normalized_containers: list[dict[str, Any]] = []
    total_teu = 0
    for container_index, container in enumerate(raw_containers):
        container_location = f"{location}.containers[{container_index}]"
        if not isinstance(container, dict):
            validation.add(container_location, "container must be an object")
            continue
        reject_unknown_keys(container, {"id", "lengthFt", "color"}, container_location, validation)
        length_ft = container.get("lengthFt")
        if length_ft not in CONTAINER_LENGTHS_FT:
            validation.add(f"{container_location}.lengthFt", "must be 20 or 40")
            continue
        normalized_container: dict[str, Any] = {"lengthFt": length_ft}
        total_teu += int(length_ft) // 20
        if "id" in container:
            container_id = trimmed_text(container.get("id"))
            validation.require(bool(container_id), f"{container_location}.id", "id must be non-empty")
            if container_id in container_ids:
                validation.add(f"{container_location}.id", f"duplicate container id {container_id}")
            elif container_id:
                container_ids.add(container_id)
                normalized_container["id"] = container_id
        if "color" in container:
            color = container.get("color")
            validation.require(valid_color(color), f"{container_location}.color", "must use #RRGGBB format")
            if valid_color(color):
                normalized_container["color"] = color.upper()
        normalized_containers.append(normalized_container)
    capacity = (
        CONTAINER_CAPACITY_TEU["gondola"]
        if wagon_type == "gondola"
        else CONTAINER_CAPACITY_TEU.get(platform_length_ft, 0)
    )
    validation.require(
        total_teu <= capacity,
        f"{location}.containers",
        f"container load is {total_teu} TEU but capacity is {capacity} TEU",
    )
    return {
        "kind": kind,
        "containers": normalized_containers,
        "usedTeu": total_teu,
        "capacityTeu": capacity,
    }


def collect_railways(
    visual_document: Any,
    branches_document: Any,
    trains_document: Any,
    crs: str,
    validation: Validation,
) -> dict[str, Any]:
    visual_paths = collect_line_paths(
        visual_document,
        "railways_visual_v4.geojson",
        crs,
        False,
        validation,
    )
    branches = collect_line_paths(
        branches_document,
        "railway_branches_v4.geojson",
        crs,
        True,
        validation,
    )
    branches_by_id = {branch["id"]: branch for branch in branches}
    validate_branch_overlay(branches, visual_paths, validation)

    if not isinstance(trains_document, dict):
        validation.add("trains-v4.json", "root must be an object")
        trains_document = {}
    reject_unknown_keys(trains_document, {"schemaVersion", "trains"}, "trains-v4.json", validation)
    validation.require(trains_document.get("schemaVersion") == "4.0", "trains-v4.json", "schemaVersion must be 4.0")
    raw_trains = trains_document.get("trains")
    if not isinstance(raw_trains, list):
        validation.add("trains-v4.json.trains", "trains must be an array")
        raw_trains = []

    train_ids: set[str] = set()
    wagon_ids: set[str] = set()
    container_ids: set[str] = set()
    normalized_trains: list[dict[str, Any]] = []
    branch_intervals: dict[str, list[tuple[float, float, str, str]]] = {}
    for train_index, train in enumerate(raw_trains):
        location = f"trains-v4.json.trains[{train_index}]"
        if not isinstance(train, dict):
            validation.add(location, "train must be an object")
            continue
        reject_unknown_keys(
            train,
            {"id", "name", "branchId", "offsetM", "direction", "gapM", "wagons"},
            location,
            validation,
        )
        train_id = trimmed_text(train.get("id"))
        name = trimmed_text(train.get("name"))
        branch_id = trimmed_text(train.get("branchId"))
        offset = train.get("offsetM")
        direction = train.get("direction")
        gap = train.get("gapM", DEFAULT_TRAIN_GAP_M)
        validation.require(bool(train_id), f"{location}.id", "id is required")
        validation.require(bool(name), f"{location}.name", "name is required")
        if train_id in train_ids:
            validation.add(f"{location}.id", f"duplicate train id {train_id}")
        elif train_id:
            train_ids.add(train_id)
        validation.require(branch_id in branches_by_id, f"{location}.branchId", f"unknown branchId {branch_id!r}")
        validation.require(number(offset), f"{location}.offsetM", "must be a finite number")
        validation.require(direction in ("forward", "reverse"), f"{location}.direction", "must be forward or reverse")
        validation.require(number(gap) and float(gap) >= 0, f"{location}.gapM", "must be a non-negative finite number")
        raw_wagons = train.get("wagons")
        if not isinstance(raw_wagons, list) or not raw_wagons:
            validation.add(f"{location}.wagons", "non-empty wagons array is required")
            continue

        sign = 1.0 if direction == "forward" else -1.0
        chainage = float(offset) if number(offset) else 0.0
        normalized_wagons: list[dict[str, Any]] = []
        previous_length = 0.0
        train_start = math.inf
        train_end = -math.inf
        branch = branches_by_id.get(branch_id)
        for wagon_index, wagon in enumerate(raw_wagons):
            wagon_location = f"{location}.wagons[{wagon_index}]"
            if not isinstance(wagon, dict):
                validation.add(wagon_location, "wagon must be an object")
                continue
            reject_unknown_keys(
                wagon,
                {"id", "type", "platformLengthFt", "loadStatus", "cargo"},
                wagon_location,
                validation,
            )
            wagon_id = trimmed_text(wagon.get("id"))
            wagon_type = wagon.get("type")
            load_status = wagon.get("loadStatus")
            platform_length_ft = wagon.get("platformLengthFt")
            validation.require(bool(wagon_id), f"{wagon_location}.id", "id is required")
            if wagon_id in wagon_ids:
                validation.add(f"{wagon_location}.id", f"duplicate wagon id {wagon_id}")
            elif wagon_id:
                wagon_ids.add(wagon_id)
            if wagon_type not in WAGON_TYPES:
                validation.add(f"{wagon_location}.type", f"unsupported wagon type {wagon_type!r}")
                continue
            validation.require(load_status in LOAD_STATUSES, f"{wagon_location}.loadStatus", "must be empty or loaded")
            if wagon_type == "fittingPlatform":
                validation.require(
                    platform_length_ft in PLATFORM_LENGTHS_FT,
                    f"{wagon_location}.platformLengthFt",
                    "must be 40, 60 or 80",
                )
                if platform_length_ft not in PLATFORM_LENGTHS_FT:
                    continue
            elif "platformLengthFt" in wagon:
                validation.add(f"{wagon_location}.platformLengthFt", "only fittingPlatform may define platformLengthFt")

            defaults = wagon_defaults(str(wagon_type), platform_length_ft)
            wagon_length = float(defaults["lengthM"])
            if wagon_index == 0:
                chainage += wagon_length / 2
            else:
                chainage += previous_length / 2 + float(gap) + wagon_length / 2
            previous_length = wagon_length
            interval_start = chainage - wagon_length / 2
            interval_end = chainage + wagon_length / 2
            train_start = min(train_start, interval_start)
            train_end = max(train_end, interval_end)
            if branch is not None:
                validation.require(
                    interval_start >= -1e-9 and interval_end <= float(branch["lengthM"]) + 1e-9,
                    wagon_location,
                    (
                        f"wagon interval [{interval_start:.3f}, {interval_end:.3f}] m "
                        f"does not fit branch {branch_id} length {branch['lengthM']:.3f} m"
                    ),
                )
                position, tangent = interpolate_path(branch["coordinates"], chainage)
            else:
                position, tangent = [0.0, 0.0], [1.0, 0.0]
            directed_tangent = [round(sign * tangent[0], 6), round(sign * tangent[1], 6)]
            heading = round(math.degrees(math.atan2(directed_tangent[1], directed_tangent[0])), 3)

            cargo: dict[str, Any] | None = None
            if load_status == "empty":
                validation.require("cargo" not in wagon, f"{wagon_location}.cargo", "empty wagon must not define cargo")
            elif load_status == "loaded":
                cargo = normalize_cargo(
                    wagon.get("cargo"),
                    str(wagon_type),
                    platform_length_ft,
                    f"{wagon_location}.cargo",
                    container_ids,
                    validation,
                )
            normalized_wagon: dict[str, Any] = {
                "id": wagon_id,
                "type": wagon_type,
                "loadStatus": load_status,
                "chainageM": round(chainage, 3),
                "intervalM": [round(interval_start, 3), round(interval_end, 3)],
                "position": position,
                "tangent": directed_tangent,
                "headingDeg": heading,
                "size": {
                    "lengthM": defaults["lengthM"],
                    "widthM": defaults["widthM"],
                    "heightM": defaults["heightM"],
                },
                "color": defaults["color"],
            }
            if wagon_type == "fittingPlatform":
                normalized_wagon["platformLengthFt"] = platform_length_ft
            if cargo is not None:
                normalized_wagon["cargo"] = cargo
            normalized_wagons.append(normalized_wagon)

        if normalized_wagons:
            train_interval = [round(train_start, 3), round(train_end, 3)]
            normalized_train = {
                "id": train_id,
                "name": name,
                "branchId": branch_id,
                "offsetM": float(offset) if number(offset) else 0.0,
                "direction": direction,
                "gapM": float(gap) if number(gap) and float(gap) >= 0 else DEFAULT_TRAIN_GAP_M,
                "intervalM": train_interval,
                "wagons": normalized_wagons,
            }
            normalized_trains.append(normalized_train)
            if branch is not None:
                intervals = branch_intervals.setdefault(branch_id, [])
                intervals.append((train_start, train_end, train_id, location))

    for branch_id, intervals in branch_intervals.items():
        intervals.sort(key=lambda value: (value[0], value[1]))
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] < previous[1] - 1e-9:
                validation.add(
                    f"{current[3]}.branchId",
                    (
                        f"train {current[2]} interval [{current[0]:.3f}, {current[1]:.3f}] m "
                        f"overlaps train {previous[2]} interval [{previous[0]:.3f}, {previous[1]:.3f}] m "
                        f"on branch {branch_id}"
                    ),
                )

    return {
        "schemaVersion": "4.0",
        "crs": crs,
        "defaults": {
            "railGaugeM": DEFAULT_RAIL_GAUGE_M,
            "railColor": DEFAULT_RAIL_COLOR,
            "trainGapM": DEFAULT_TRAIN_GAP_M,
            "wagonTypes": {
                **{
                    wagon_type: defaults
                    for wagon_type, defaults in WAGON_DEFAULTS.items()
                    if wagon_type != "fittingPlatform"
                },
                "fittingPlatform": [
                    {
                        "platformLengthFt": int(platform_length_ft),
                        **defaults,
                    }
                    for platform_length_ft, defaults in WAGON_DEFAULTS["fittingPlatform"].items()
                ],
            },
        },
        "visualPaths": visual_paths,
        "branches": branches,
        "trains": normalized_trains,
    }


def collect_cranes(
    portal_document: Any,
    yard_document: Any,
    layout: Any,
    crs: str,
    validation: Validation,
) -> dict[str, Any]:
    cranes: list[dict[str, Any]] = []
    crane_ids: set[str] = set()

    for feature_index, feature in enumerate(
        feature_collection(portal_document, "portal_cranes_v4.geojson", validation)
    ):
        location = f"portal_cranes_v4.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            validation.add(f"{location}.properties", "properties object is required")
            continue
        crane_id = trimmed_text(properties.get("id"))
        name = trimmed_text(properties.get("name"))
        model = trimmed_text(properties.get("model"))
        validation.require(bool(crane_id), f"{location}.properties.id", "id is required")
        validation.require(bool(name), f"{location}.properties.name", "name is required")
        validation.require(bool(model), f"{location}.properties.model", "model is required")
        if crane_id in crane_ids:
            validation.add(f"{location}.properties.id", f"duplicate crane id {crane_id}")
        elif crane_id:
            crane_ids.add(crane_id)
        azimuth = properties.get("azimuthDeg")
        scale = properties.get("scale")
        color = properties.get("color")
        validation.require(
            number(azimuth) and 0 <= float(azimuth) < 360,
            f"{location}.properties.azimuthDeg",
            "must be in range [0, 360)",
        )
        validation.require(number(scale) and float(scale) > 0, f"{location}.properties.scale", "must be positive")
        validation.require(valid_color(color), f"{location}.properties.color", "must use #RRGGBB format")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            validation.add(f"{location}.geometry", "geometry must be Point")
            continue
        coordinates = point(geometry.get("coordinates"))
        if coordinates is None:
            validation.add(f"{location}.geometry.coordinates", "coordinates must be a finite point")
            continue
        if crane_id:
            cranes.append(
                {
                    "id": crane_id,
                    "name": name,
                    "type": "portal",
                    "model": model,
                    "position": list(coordinates),
                    "azimuthDeg": azimuth,
                    "scale": scale,
                    "color": color,
                }
            )
    validation.require(crs_code(portal_document) == crs, "portal_cranes_v4.geojson", f"CRS must be {crs}")

    if not isinstance(yard_document, dict):
        validation.add("yard-cranes-v4.json", "root must be an object")
        return {"schemaVersion": "4.0", "crs": crs, "cranes": cranes}
    validation.require(yard_document.get("schemaVersion") == "4.0", "yard-cranes-v4.json", "schemaVersion must be 4.0")
    yard_cranes = yard_document.get("cranes")
    if not isinstance(yard_cranes, list):
        validation.add("yard-cranes-v4.json.cranes", "cranes must be an array")
        return {"schemaVersion": "4.0", "crs": crs, "cranes": cranes}

    sites_by_id = {
        str(site.get("id", "")): site
        for site in layout.get("sites", [])
        if isinstance(site, dict)
    } if isinstance(layout, dict) else {}
    for crane_index, crane in enumerate(yard_cranes):
        location = f"yard-cranes-v4.json.cranes[{crane_index}]"
        if not isinstance(crane, dict):
            validation.add(location, "crane must be an object")
            continue
        crane_id = trimmed_text(crane.get("id"))
        name = trimmed_text(crane.get("name"))
        crane_type = crane.get("type")
        site_id = trimmed_text(crane.get("siteId"))
        movement_axis = crane.get("movementAxis")
        validation.require(bool(crane_id), f"{location}.id", "id is required")
        validation.require(bool(name), f"{location}.name", "name is required")
        if crane_id in crane_ids:
            validation.add(f"{location}.id", f"duplicate crane id {crane_id}")
        elif crane_id:
            crane_ids.add(crane_id)
        validation.require(crane_type in ("rtg", "rmg"), f"{location}.type", "must be rtg or rmg")
        validation.require(site_id in sites_by_id, f"{location}.siteId", f"unknown siteId {site_id!r}")
        validation.require(movement_axis in ("bays", "rows"), f"{location}.movementAxis", "must be bays or rows")
        validation.require(number(crane.get("positionM")), f"{location}.positionM", "must be a finite number")
        validation.require(valid_color(crane.get("color")), f"{location}.color", "must use #RRGGBB format")
        if crane_type == "rmg":
            rail_inset = crane.get("railInsetM")
            validation.require(
                number(rail_inset) and float(rail_inset) >= 0,
                f"{location}.railInsetM",
                "must be non-negative",
            )
            for wing_field in ("leftWingM", "rightWingM"):
                wing_size = crane.get(wing_field)
                validation.require(
                    number(wing_size) and float(wing_size) >= 0,
                    f"{location}.{wing_field}",
                    "must be a non-negative finite number",
                )
        if crane_id:
            normalized = {
                "id": crane_id,
                "name": name,
                "type": crane_type,
                "siteId": site_id,
                "movementAxis": movement_axis,
                "positionM": crane.get("positionM"),
                "color": crane.get("color"),
            }
            if crane_type == "rmg":
                normalized["railInsetM"] = crane.get("railInsetM")
                normalized["leftWingM"] = crane.get("leftWingM")
                normalized["rightWingM"] = crane.get("rightWingM")
            cranes.append(normalized)

    return {"schemaVersion": "4.0", "crs": crs, "cranes": cranes}


def inclusive_numbers(segment: Any, location: str, validation: Validation) -> list[int]:
    numbers = segment.get("numbers") if isinstance(segment, dict) else None
    if not isinstance(numbers, dict):
        validation.add(location, "numbers object is required")
        return []
    first = numbers.get("from")
    last = numbers.get("to")
    if not isinstance(first, int) or isinstance(first, bool) or not isinstance(last, int) or isinstance(last, bool):
        validation.add(location, "numbers.from/to must be integers")
        return []
    if first <= 0 or last < first:
        validation.add(location, "numbers must be a positive inclusive range")
        return []
    return list(range(first, last + 1))


def validate_layout(
    layout: Any,
    sites: dict[str, dict[str, Any]],
    axes: dict[str, dict[str, Any]],
    validation: Validation,
) -> list[str]:
    changes: list[str] = []
    if not isinstance(layout, dict):
        validation.add("terminal-layout-v4.json", "root must be an object")
        return changes
    validation.require(layout.get("schemaVersion") == "4.0", "terminal-layout-v4.json", "schemaVersion must be 4.0")
    terminal = layout.get("terminal")
    if not isinstance(terminal, dict):
        validation.add("terminal-layout-v4.json.terminal", "terminal object is required")
        return changes
    crs = terminal.get("crs")
    validation.require(terminal.get("units") == "m", "terminal-layout-v4.json.terminal", "units must be m")
    validation.require(isinstance(crs, str) and bool(crs), "terminal-layout-v4.json.terminal", "crs is required")

    cell = layout.get("cell")
    if not isinstance(cell, dict):
        validation.add("terminal-layout-v4.json.cell", "cell object is required")
        return changes
    validation.require(cell.get("layoutMode") == "fixed_pitch", "terminal-layout-v4.json.cell", "layoutMode must be fixed_pitch")
    bay_pitch = cell.get("bayPitchM")
    row_pitch = cell.get("rowPitchM")
    gap = cell.get("minimumGapM")
    container = cell.get("container20Ft")
    for key, value in (("bayPitchM", bay_pitch), ("rowPitchM", row_pitch), ("minimumGapM", gap)):
        validation.require(number(value) and value > 0, f"terminal-layout-v4.json.cell.{key}", "must be positive")
    if isinstance(container, dict) and all(number(container.get(key)) for key in ("lengthM", "widthM")) and number(gap):
        validation.require(
            abs(float(bay_pitch) - (float(container["lengthM"]) + float(gap))) <= 1e-9,
            "terminal-layout-v4.json.cell",
            "bayPitchM must equal container length plus minimum gap",
        )
        validation.require(
            abs(float(row_pitch) - (float(container["widthM"]) + float(gap))) <= 1e-9,
            "terminal-layout-v4.json.cell",
            "rowPitchM must equal container width plus minimum gap",
        )
    else:
        validation.add("terminal-layout-v4.json.cell.container20Ft", "lengthM and widthM are required")

    demo = layout.get("demo")
    if not isinstance(demo, dict):
        validation.add("terminal-layout-v4.json.demo", "demo object is required")
    else:
        validation.require(isinstance(demo.get("enabled"), bool), "terminal-layout-v4.json.demo.enabled", "must be boolean")
        validation.require(
            isinstance(demo.get("seed"), int) and not isinstance(demo.get("seed"), bool),
            "terminal-layout-v4.json.demo.seed",
            "must be an integer",
        )
        for key in ("fillPercent", "container40FtPercent"):
            value = demo.get(key)
            validation.require(
                isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100,
                f"terminal-layout-v4.json.demo.{key}",
                "must be an integer from 0 to 100",
            )
        validation.require(
            isinstance(demo.get("maxTier"), int)
            and not isinstance(demo.get("maxTier"), bool)
            and demo.get("maxTier") > 0,
            "terminal-layout-v4.json.demo.maxTier",
            "must be a positive integer",
        )

    layout_sites = layout.get("sites")
    if not isinstance(layout_sites, list) or not layout_sites:
        validation.add("terminal-layout-v4.json.sites", "non-empty sites array is required")
        return changes
    layout_ids: set[str] = set()
    for site_index, site in enumerate(layout_sites):
        location = f"terminal-layout-v4.json.sites[{site_index}]"
        if not isinstance(site, dict):
            validation.add(location, "site must be an object")
            continue
        site_id = str(site.get("id", "")).strip()
        if not validation.require(bool(site_id), location, "id is required"):
            continue
        if site_id in layout_ids:
            validation.add(location, f"duplicate site id {site_id}")
        layout_ids.add(site_id)
        validation.require(site.get("qgisFeatureId") == site_id, location, "qgisFeatureId must equal id")
        validation.require(
            site.get("containerLabelRotationDeg") in (0, 180),
            f"{location}.containerLabelRotationDeg",
            "must be 0 or 180",
        )

        zone_ids: set[str] = set()
        zone_id_list: list[str] = []
        zones = site.get("zones")
        if not isinstance(zones, list) or not zones:
            validation.add(f"{location}.zones", "non-empty zones array is required")
            zones = []
        for zone_index, zone in enumerate(zones):
            zone_id = str(zone.get("id", "")).strip() if isinstance(zone, dict) else ""
            if not zone_id:
                validation.add(f"{location}.zones[{zone_index}]", "id is required")
            elif zone_id in zone_ids:
                validation.add(f"{location}.zones[{zone_index}]", f"duplicate zone id {zone_id}")
            else:
                zone_id_list.append(zone_id)
            zone_ids.add(zone_id)
            validation.require(
                isinstance(zone, dict) and zone.get("kind") in ("storage", "discharge"),
                f"{location}.zones[{zone_index}].kind",
                "must be storage or discharge",
            )

        topology = site.get("topology")
        if not isinstance(topology, dict):
            validation.add(f"{location}.topology", "topology object is required")
            continue
        row_numbers: set[int] = set()
        row_count = 0
        rows_footprint = 0.0
        rows = topology.get("rows")
        if not isinstance(rows, list) or not rows:
            validation.add(f"{location}.topology.rows", "non-empty rows array is required")
            rows = []
        for segment_index, segment in enumerate(rows):
            segment_location = f"{location}.topology.rows[{segment_index}]"
            kind = segment.get("kind") if isinstance(segment, dict) else None
            if kind == "cells":
                values = inclusive_numbers(segment, segment_location, validation)
                duplicates = row_numbers.intersection(values)
                if duplicates:
                    validation.add(segment_location, f"duplicate Row numbers {sorted(duplicates)}")
                row_numbers.update(values)
                row_count += len(values)
                rows_footprint += len(values) * float(row_pitch or 0)
            elif kind in ("aisle", "reeferRack", "canopy"):
                width = segment.get("widthM")
                validation.require(number(width) and width > 0, segment_location, "widthM must be positive")
                validation.require(segment.get("runsAlong") == "bays", segment_location, "runsAlong must be bays")
                if number(width):
                    rows_footprint += float(width)
            else:
                validation.add(segment_location, f"unsupported kind {kind!r}")

        zone_numbers: dict[str, set[int]] = {zone_id: set() for zone_id in zone_ids}
        bay_count = 0
        bays_footprint = 0.0
        bays = topology.get("bays")
        if not isinstance(bays, list) or not bays:
            validation.add(f"{location}.topology.bays", "non-empty bays array is required")
            bays = []
        for segment_index, segment in enumerate(bays):
            segment_location = f"{location}.topology.bays[{segment_index}]"
            kind = segment.get("kind") if isinstance(segment, dict) else None
            if kind == "zone":
                zone_id = str(segment.get("zoneId", ""))
                if zone_id not in zone_ids:
                    validation.add(segment_location, f"unknown zoneId {zone_id}")
                values = inclusive_numbers(segment, segment_location, validation)
                known = zone_numbers.setdefault(zone_id, set())
                duplicates = known.intersection(values)
                if duplicates:
                    validation.add(segment_location, f"duplicate Bay numbers in zone {zone_id}: {sorted(duplicates)}")
                known.update(values)
                bay_count += len(values)
                bays_footprint += len(values) * float(bay_pitch or 0)
            elif kind in ("aisle", "reeferRack", "canopy"):
                width = segment.get("widthM")
                validation.require(number(width) and width > 0, segment_location, "widthM must be positive")
                validation.require(segment.get("runsAlong") == "rows", segment_location, "runsAlong must be rows")
                if number(width):
                    bays_footprint += float(width)
            else:
                validation.add(segment_location, f"unsupported kind {kind!r}")

        calculated_expected = {
            "rowCellCount": row_count,
            "bayCellCount": bay_count,
            "zoneCellCounts": {
                zone_id: len(zone_numbers.get(zone_id, set())) * row_count for zone_id in zone_id_list
            },
            "footprintAlongRowsM": round(rows_footprint, 2),
            "footprintAlongBaysM": round(bays_footprint, 2),
        }
        if site.get("expected") != calculated_expected:
            site["expected"] = calculated_expected
            changes.append(f"{site_id}: updated expected values")

        site_geometry = sites.get(site_id)
        axis_geometry = axes.get(site_id)
        if site_geometry is None:
            validation.add(location, f"no container site polygon for {site_id}")
        if axis_geometry is None:
            validation.add(location, f"no bay axis for {site_id}")
        if site_geometry is not None and axis_geometry is not None and len(site_geometry["vertices"]) == 4:
            vertices = site_geometry["vertices"]
            axis = axis_geometry["points"]
            vertex_indexes: list[int] = []
            endpoints_match = True
            for axis_point in axis:
                nearest = min(range(4), key=lambda index: distance(axis_point, vertices[index]))
                if distance(axis_point, vertices[nearest]) > AXIS_VERTEX_TOLERANCE_M:
                    validation.add(location, "bay axis endpoints must match polygon vertices")
                    endpoints_match = False
                vertex_indexes.append(nearest)
            axis_follows_side = len(vertex_indexes) == 2 and (
                (vertex_indexes[0] - vertex_indexes[1]) % 4 in (1, 3)
            )
            if len(vertex_indexes) == 2:
                validation.require(
                    axis_follows_side,
                    location,
                    "bay axis must follow one polygon side",
                )
            bay_extent = distance(axis[0], axis[1])
            origin_index = vertex_indexes[0] if vertex_indexes else 0
            adjacent_indexes = ((origin_index - 1) % 4, (origin_index + 1) % 4)
            row_extents = [
                distance(vertices[origin_index], vertices[index])
                for index in adjacent_indexes
                if index != (vertex_indexes[1] if len(vertex_indexes) > 1 else -1)
            ]
            row_extent = row_extents[0] if row_extents else 0.0
            can_resize = (
                endpoints_match
                and axis_follows_side
                and bay_extent > GEOMETRY_TOLERANCE_M
                and row_extent > GEOMETRY_TOLERANCE_M
            )
            needs_bay_resize = abs(bays_footprint - bay_extent) > RESIZE_TOLERANCE_M
            needs_row_resize = abs(rows_footprint - row_extent) > RESIZE_TOLERANCE_M
            if can_resize and (needs_bay_resize or needs_row_resize):
                axis_end_index = vertex_indexes[1]
                row_end_index = next(index for index in adjacent_indexes if index != axis_end_index)
                opposite_index = next(
                    index for index in range(4) if index not in (origin_index, axis_end_index, row_end_index)
                )
                origin_point = vertices[origin_index]
                bay_unit = (
                    (vertices[axis_end_index][0] - origin_point[0]) / bay_extent,
                    (vertices[axis_end_index][1] - origin_point[1]) / bay_extent,
                )
                row_unit = (
                    (vertices[row_end_index][0] - origin_point[0]) / row_extent,
                    (vertices[row_end_index][1] - origin_point[1]) / row_extent,
                )
                required_bay_extent = bays_footprint if needs_bay_resize else bay_extent
                required_row_extent = rows_footprint if needs_row_resize else row_extent
                expanded_vertices = list(vertices)
                expanded_vertices[axis_end_index] = (
                    origin_point[0] + bay_unit[0] * required_bay_extent,
                    origin_point[1] + bay_unit[1] * required_bay_extent,
                )
                expanded_vertices[row_end_index] = (
                    origin_point[0] + row_unit[0] * required_row_extent,
                    origin_point[1] + row_unit[1] * required_row_extent,
                )
                expanded_vertices[opposite_index] = (
                    expanded_vertices[axis_end_index][0] + row_unit[0] * required_row_extent,
                    expanded_vertices[axis_end_index][1] + row_unit[1] * required_row_extent,
                )
                expanded_vertices = [
                    (round(x, COORDINATE_PRECISION), round(y, COORDINATE_PRECISION))
                    for x, y in expanded_vertices
                ]
                ring = [[x, y] for x, y in expanded_vertices]
                ring.append(ring[0].copy())
                site_geometry["feature"]["geometry"]["coordinates"][0] = ring
                axis_geometry["feature"]["geometry"]["coordinates"] = [
                    list(origin_point),
                    list(expanded_vertices[axis_end_index]),
                ]
                site_geometry["vertices"] = expanded_vertices
                axis_geometry["points"] = [origin_point, expanded_vertices[axis_end_index]]
                changes.append(
                    f"{site_id}: resized site from {bay_extent:.2f} x {row_extent:.2f} m "
                    f"to {required_bay_extent:.2f} x {required_row_extent:.2f} m"
                )

    validation.require_ids(
        set(sites),
        layout_ids,
        "container_sites_v4.geojson",
        "site ids must exactly match layout",
        actual_label="container_sites_v4.geojson",
        expected_label="terminal-layout-v4.json",
    )
    validation.require_ids(
        set(axes),
        layout_ids,
        "container_site_bay_axes_v4.geojson",
        "site ids must exactly match layout",
        actual_label="container_site_bay_axes_v4.geojson",
        expected_label="terminal-layout-v4.json",
    )
    return changes


def validate_declared_paths(layout: Any, validation: Validation) -> None:
    try:
        qgis = layout["qgis"]
        declared = {
            "sceneOrigin": qgis["sceneOriginFile"],
            "containerSites": qgis["siteLayer"]["file"],
            "bayAxes": qgis["bayAxisLayer"]["file"],
        }
    except (KeyError, TypeError):
        validation.add("terminal-layout-v4.json.qgis", "scene origin and QGIS layer declarations are required")
        return
    expected = {
        "sceneOrigin": "data/scene_origin.txt",
        "containerSites": "data/geojson/container_sites_v4.geojson",
        "bayAxes": "data/geojson/container_site_bay_axes_v4.geojson",
    }
    for key, expected_path in expected.items():
        validation.require(declared.get(key) == expected_path, f"terminal-layout-v4.json.qgis.{key}", f"must be {expected_path}")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build(data_dir: Path, templates_dir: Path, check_only: bool) -> None:
    validation = Validation()
    source_texts: dict[str, str] = {}
    source_documents: dict[str, Any] = {}
    for key, relative_path in SOURCE_FILES.items():
        path = data_dir / relative_path
        text = read_text(path, validation)
        source_texts[key] = text
        if path.suffix.lower() in (".json", ".geojson"):
            source_documents[key] = read_json(path, text, validation)

    origin = parse_scene_origin(source_texts["sceneOrigin"], validation)
    layout = source_documents.get("terminalLayout", {})
    terminal_crs = ""
    if isinstance(layout, dict) and isinstance(layout.get("terminal"), dict):
        terminal_crs = str(layout["terminal"].get("crs", ""))
    validation.require(bool(terminal_crs), "terminal-layout-v4.json.terminal.crs", "CRS is required")
    validation.require(origin.get("crs") == terminal_crs, "scene_origin.txt", f"CRS must be {terminal_crs}")

    validate_declared_paths(layout, validation)
    validate_shoreline(source_documents.get("shoreline", {}), terminal_crs, validation)
    validate_water(source_documents.get("water", {}), terminal_crs, validation)
    validate_buildings(source_documents.get("buildings", {}), terminal_crs, validation)
    validate_roads(source_documents.get("roads", {}), terminal_crs, validation)
    validate_port_boundary(source_documents.get("portBoundary", {}), terminal_crs, validation)
    berths_document = collect_berths(
        source_documents.get("berth", {}),
        terminal_crs,
        validation,
    )
    berth_ids = {
        str(berth.get("id", ""))
        for berth in berths_document.get("berths", [])
        if isinstance(berth, dict)
    }
    validate_vessels(source_documents.get("vessels", {}), berth_ids, validation)
    berth_vessel_ids: set[str] = set()
    vessels_document = source_documents.get("vessels", {})
    if isinstance(vessels_document, dict) and isinstance(vessels_document.get("vessels"), list):
        for vessel in vessels_document["vessels"]:
            if isinstance(vessel, dict):
                vessel_id = trimmed_text(vessel.get("id"))
                if vessel_id:
                    berth_vessel_ids.add(vessel_id)
    validate_anchorage(
        source_documents.get("anchorage", {}),
        source_documents.get("water", {}),
        terminal_crs,
        validation,
    )
    validate_anchorage_vessels(
        source_documents.get("anchorageVessels", {}),
        berth_vessel_ids,
        berth_ids,
        validation,
    )
    sites = collect_sites(source_documents.get("containerSites", {}), terminal_crs, validation)
    axes = collect_axes(source_documents.get("bayAxes", {}), terminal_crs, validation)
    changes = validate_layout(layout, sites, axes, validation)
    cranes_document = collect_cranes(
        source_documents.get("portalCranes", {}),
        source_documents.get("yardCranes", {}),
        layout,
        terminal_crs,
        validation,
    )
    railways_document = collect_railways(
        source_documents.get("railwaysVisual", {}),
        source_documents.get("railwayBranches", {}),
        source_documents.get("trains", {}),
        terminal_crs,
        validation,
    )
    validate_1c_property_names(railways_document, "railways-v4.json", validation)
    validation.finish()

    normalized = {
        key: canonical_json(source_documents[key])
        if key in source_documents
        else source_texts[key].replace("\r\n", "\n").rstrip() + "\n"
        for key in SOURCE_FILES
    }
    normalized["berths"] = canonical_json(berths_document)
    normalized["cranes"] = canonical_json(cranes_document)
    normalized["railways"] = canonical_json(railways_document)
    normalized["anchorageVessels"] = source_texts["anchorageVessels"]
    berths_path = data_dir / "berths-v4.json"
    cranes_path = data_dir / "cranes-v4.json"
    railways_path = data_dir / "railways-v4.json"
    try:
        current_berths = berths_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except OSError:
        current_berths = ""
    if current_berths != normalized["berths"]:
        changes.append("updated derived berths-v4.json")
    try:
        current_cranes = cranes_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except OSError:
        current_cranes = ""
    if current_cranes != normalized["cranes"]:
        changes.append("updated derived cranes-v4.json")
    try:
        current_railways = railways_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except OSError:
        current_railways = ""
    if current_railways != normalized["railways"]:
        changes.append("updated derived railways-v4.json")

    hashes = {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in normalized.items()}
    build_id = hashlib.sha256("".join(hashes[key] for key in sorted(hashes)).encode("ascii")).hexdigest()[:16]
    manifest_paths = dict(SOURCE_FILES)
    manifest_paths["berths"] = "berths-v4.json"
    manifest_paths["cranes"] = "cranes-v4.json"
    manifest_paths["railways"] = "railways-v4.json"
    manifest = canonical_json(
        {
            "schemaVersion": "4.0",
            "buildId": build_id,
            "algorithm": "sha256",
            "sources": {
                key: {
                    "path": manifest_paths[key].replace("\\", "/"),
                    "sha256": hashes[key],
                    "characters": len(normalized[key]),
                }
                for key in sorted(normalized)
            },
        }
    )

    if not check_only:
        for key in ("terminalLayout", "containerSites", "bayAxes"):
            atomic_write(data_dir / SOURCE_FILES[key], normalized[key])
        atomic_write(berths_path, normalized["berths"])
        atomic_write(cranes_path, normalized["cranes"])
        atomic_write(railways_path, normalized["railways"])
        for key, template_path in TEMPLATE_PATHS.items():
            if key in normalized:
                atomic_write(templates_dir / template_path, normalized[key])
        atomic_write(templates_dir / TEMPLATE_PATHS["manifest"], manifest)

    site_count = len(layout.get("sites", [])) if isinstance(layout, dict) else 0
    berth_count = len(berths_document.get("berths", []))
    vessel_count = len(source_documents.get("vessels", {}).get("vessels", []))
    anchorage_vessel_count = len(source_documents.get("anchorageVessels", {}).get("vessels", []))
    crane_count = len(cranes_document.get("cranes", []))
    branch_count = len(railways_document.get("branches", []))
    train_count = len(railways_document.get("trains", []))
    wagon_count = sum(
        len(train.get("wagons", []))
        for train in railways_document.get("trains", [])
        if isinstance(train, dict)
    )
    action = "validated" if check_only else "synchronized"
    print(
        f"V4 scene {action}: buildId={build_id} "
        f"sites={site_count} berths={berth_count} vessels={vessel_count} "
        f"anchorageVessels={anchorage_vessel_count} cranes={crane_count} "
        f"railwayBranches={branch_count} trains={train_count} wagons={wagon_count}"
    )
    for change in changes:
        prefix = "would change" if check_only else "changed"
        print(f"  {prefix}: {change}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--templates-dir", type=Path, default=DEFAULT_TEMPLATES_DIR)
    parser.add_argument("--check", action="store_true", help="validate without updating 1C templates")
    args = parser.parse_args()
    build(args.data_dir.resolve(), args.templates_dir.resolve(), args.check)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
