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
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_TEMPLATES_DIR = ROOT / "src" / "DataProcessors" / "d3_v4" / "Templates"
AXIS_VERTEX_TOLERANCE_M = 0.15
GEOMETRY_TOLERANCE_M = 0.05
COORDINATE_PRECISION = 2
RESIZE_TOLERANCE_M = math.sqrt(2) * 0.5 * 10**-COORDINATE_PRECISION + 1e-9

SOURCE_FILES = {
    "terminalLayout": "terminal-layout-v4.json",
    "sceneOrigin": "scene_origin.txt",
    "shoreline": "geojson/shoreline.geojson",
    "water": "geojson/water.geojson",
    "buildings": "geojson/buildings.geojson",
    "berthPoints": "geojson/berth_points_v4.geojson",
    "berthHeadingAxes": "geojson/berth_heading_axes_v4.geojson",
    "vessels": "vessels-v4.json",
    "portalCranes": "geojson/portal_cranes_v4.geojson",
    "yardCranes": "yard-cranes-v4.json",
    "containerSites": "geojson/container_sites_v4.geojson",
    "bayAxes": "geojson/container_site_bay_axes_v4.geojson",
}

TEMPLATE_PATHS = {
    "terminalLayout": "TerminalLayoutV4_json/Template.txt",
    "sceneOrigin": "SceneOrigin_txt/Template.txt",
    "shoreline": "Shoreline_geojson/Template.txt",
    "water": "Water_geojson/Template.txt",
    "buildings": "Buildings_geojson/Template.txt",
    "berths": "BerthsV4_json/Template.txt",
    "vessels": "VesselsV4_json/Template.txt",
    "cranes": "CranesV4_json/Template.txt",
    "containerSites": "ContainerSitesV4_geojson/Template.txt",
    "bayAxes": "ContainerSiteBayAxesV4_geojson/Template.txt",
    "manifest": "SceneManifestV4_json/Template.txt",
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
    points_document: Any,
    axes_document: Any,
    crs: str,
    validation: Validation,
) -> dict[str, Any]:
    points: dict[str, dict[str, Any]] = {}
    for feature_index, feature in enumerate(feature_collection(points_document, "berth_points_v4.geojson", validation)):
        location = f"berth_points_v4.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        berth_id = properties.get("berth_id") if isinstance(properties, dict) else None
        name = properties.get("name") if isinstance(properties, dict) else None
        berth_id = berth_id.strip() if isinstance(berth_id, str) else ""
        name = name.strip() if isinstance(name, str) else ""
        validation.require(bool(berth_id), f"{location}.properties.berth_id", "berth_id is required")
        validation.require(bool(name), f"{location}.properties.name", "name is required")
        if berth_id in points:
            validation.add(f"{location}.properties.berth_id", f"duplicate berth_id {berth_id}")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            validation.add(f"{location}.geometry", "geometry must be Point")
            continue
        center = point(geometry.get("coordinates"))
        if center is None:
            validation.add(f"{location}.geometry.coordinates", "coordinates must be a finite point")
            continue
        if berth_id and berth_id not in points:
            points[berth_id] = {"id": berth_id, "name": name, "center": center}
    validation.require(crs_code(points_document) == crs, "berth_points_v4.geojson", f"CRS must be {crs}")

    axes: dict[str, list[tuple[float, float]]] = {}
    for feature_index, feature in enumerate(
        feature_collection(axes_document, "berth_heading_axes_v4.geojson", validation)
    ):
        location = f"berth_heading_axes_v4.geojson.features[{feature_index}]"
        properties = feature.get("properties") if isinstance(feature, dict) else None
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        berth_id = properties.get("berth_id") if isinstance(properties, dict) else None
        berth_id = berth_id.strip() if isinstance(berth_id, str) else ""
        validation.require(bool(berth_id), f"{location}.properties.berth_id", "berth_id is required")
        if berth_id in axes:
            validation.add(f"{location}.properties.berth_id", f"duplicate berth_id {berth_id}")
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
        if berth_id and berth_id not in axes:
            axes[berth_id] = axis
    validation.require(crs_code(axes_document) == crs, "berth_heading_axes_v4.geojson", f"CRS must be {crs}")

    validation.require(set(points) == set(axes), "virtual berths", "berth_id sets in points and axes must match")
    berths: list[dict[str, Any]] = []
    for berth_id, description in points.items():
        axis = axes.get(berth_id)
        if axis is None:
            continue
        validation.require(
            distance(description["center"], axis[0]) <= AXIS_VERTEX_TOLERANCE_M,
            f"virtual berths.{berth_id}",
            "heading axis must start at berth point",
        )
        berths.append(
            {
                "id": berth_id,
                "name": description["name"],
                "center": list(description["center"]),
                "headingPoint": list(axis[1]),
            }
        )
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
            vessel.get("vesselType") in ("container", "carCarrier", "coal", "fish", "general", "empty"),
            f"{location}.vesselType",
            "must be container, carCarrier, coal, fish, general or empty",
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


def valid_color(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None


def trimmed_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


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

    validation.require(set(sites) == layout_ids, "container_sites_v4.geojson", "site ids must exactly match layout")
    validation.require(set(axes) == layout_ids, "container_site_bay_axes_v4.geojson", "site ids must exactly match layout")
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
    berths_document = collect_berths(
        source_documents.get("berthPoints", {}),
        source_documents.get("berthHeadingAxes", {}),
        terminal_crs,
        validation,
    )
    berth_ids = {
        str(berth.get("id", ""))
        for berth in berths_document.get("berths", [])
        if isinstance(berth, dict)
    }
    validate_vessels(source_documents.get("vessels", {}), berth_ids, validation)
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
    validation.finish()

    normalized = {
        key: canonical_json(source_documents[key])
        if key in source_documents
        else source_texts[key].replace("\r\n", "\n").rstrip() + "\n"
        for key in SOURCE_FILES
    }
    normalized["berths"] = canonical_json(berths_document)
    normalized["cranes"] = canonical_json(cranes_document)
    berths_path = data_dir / "berths-v4.json"
    cranes_path = data_dir / "cranes-v4.json"
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

    hashes = {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in normalized.items()}
    build_id = hashlib.sha256("".join(hashes[key] for key in sorted(hashes)).encode("ascii")).hexdigest()[:16]
    manifest_paths = dict(SOURCE_FILES)
    manifest_paths["berths"] = "berths-v4.json"
    manifest_paths["cranes"] = "cranes-v4.json"
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
        for key, template_path in TEMPLATE_PATHS.items():
            if key in normalized:
                atomic_write(templates_dir / template_path, normalized[key])
        atomic_write(templates_dir / TEMPLATE_PATHS["manifest"], manifest)

    site_count = len(layout.get("sites", [])) if isinstance(layout, dict) else 0
    berth_count = len(berths_document.get("berths", []))
    vessel_count = len(source_documents.get("vessels", {}).get("vessels", []))
    crane_count = len(cranes_document.get("cranes", []))
    action = "validated" if check_only else "synchronized"
    print(
        f"V4 scene {action}: buildId={build_id} "
        f"sites={site_count} berths={berth_count} vessels={vessel_count} cranes={crane_count}"
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
