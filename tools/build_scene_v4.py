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

SOURCE_FILES = {
    "terminalLayout": "terminal-layout-v4.json",
    "sceneOrigin": "scene_origin.txt",
    "shoreline": "geojson/shoreline.geojson",
    "water": "geojson/water.geojson",
    "containerSites": "geojson/container_sites_v4.geojson",
    "bayAxes": "geojson/container_site_bay_axes_v4.geojson",
}

TEMPLATE_PATHS = {
    "terminalLayout": "TerminalLayoutV4_json/Template.txt",
    "sceneOrigin": "SceneOrigin_txt/Template.txt",
    "shoreline": "Shoreline_geojson/Template.txt",
    "water": "Water_geojson/Template.txt",
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


def collect_axes(document: Any, crs: str, validation: Validation) -> dict[str, list[tuple[float, float]]]:
    axes: dict[str, list[tuple[float, float]]] = {}
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
        axes[site_id] = axis
    validation.require(crs_code(document) == crs, "container_site_bay_axes_v4.geojson", f"CRS must be {crs}")
    return axes


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
    axes: dict[str, list[tuple[float, float]]],
    validation: Validation,
) -> None:
    if not isinstance(layout, dict):
        validation.add("terminal-layout-v4.json", "root must be an object")
        return
    validation.require(layout.get("schemaVersion") == "4.0", "terminal-layout-v4.json", "schemaVersion must be 4.0")
    terminal = layout.get("terminal")
    if not isinstance(terminal, dict):
        validation.add("terminal-layout-v4.json.terminal", "terminal object is required")
        return
    crs = terminal.get("crs")
    validation.require(terminal.get("units") == "m", "terminal-layout-v4.json.terminal", "units must be m")
    validation.require(isinstance(crs, str) and bool(crs), "terminal-layout-v4.json.terminal", "crs is required")

    cell = layout.get("cell")
    if not isinstance(cell, dict):
        validation.add("terminal-layout-v4.json.cell", "cell object is required")
        return
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
        return
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
            elif kind in ("aisle", "reeferRack"):
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
            elif kind in ("aisle", "reeferRack"):
                width = segment.get("widthM")
                validation.require(number(width) and width > 0, segment_location, "widthM must be positive")
                validation.require(segment.get("runsAlong") == "rows", segment_location, "runsAlong must be rows")
                if number(width):
                    bays_footprint += float(width)
            else:
                validation.add(segment_location, f"unsupported kind {kind!r}")

        expected = site.get("expected")
        if isinstance(expected, dict):
            validation.require(expected.get("rowCellCount") == row_count, f"{location}.expected", "rowCellCount mismatch")
            validation.require(expected.get("bayCellCount") == bay_count, f"{location}.expected", "bayCellCount mismatch")
            validation.require(
                number(expected.get("footprintAlongRowsM"))
                and abs(float(expected["footprintAlongRowsM"]) - rows_footprint) <= 0.01,
                f"{location}.expected",
                "footprintAlongRowsM mismatch",
            )
            validation.require(
                number(expected.get("footprintAlongBaysM"))
                and abs(float(expected["footprintAlongBaysM"]) - bays_footprint) <= 0.01,
                f"{location}.expected",
                "footprintAlongBaysM mismatch",
            )
            expected_zone_counts = expected.get("zoneCellCounts")
            if isinstance(expected_zone_counts, dict):
                for zone_id in zone_ids:
                    validation.require(
                        expected_zone_counts.get(zone_id) == len(zone_numbers.get(zone_id, set())) * row_count,
                        f"{location}.expected.zoneCellCounts.{zone_id}",
                        "zone cell count mismatch",
                    )
        else:
            validation.add(f"{location}.expected", "expected object is required")

        site_geometry = sites.get(site_id)
        axis = axes.get(site_id)
        if site_geometry is None:
            validation.add(location, f"no container site polygon for {site_id}")
        if axis is None:
            validation.add(location, f"no bay axis for {site_id}")
        if site_geometry is not None and axis is not None and len(site_geometry["vertices"]) == 4:
            vertices = site_geometry["vertices"]
            vertex_indexes: list[int] = []
            for axis_point in axis:
                nearest = min(range(4), key=lambda index: distance(axis_point, vertices[index]))
                if distance(axis_point, vertices[nearest]) > AXIS_VERTEX_TOLERANCE_M:
                    validation.add(location, "bay axis endpoints must match polygon vertices")
                vertex_indexes.append(nearest)
            if len(vertex_indexes) == 2:
                validation.require(
                    (vertex_indexes[0] - vertex_indexes[1]) % 4 in (1, 3),
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
            validation.require(
                bay_extent + GEOMETRY_TOLERANCE_M >= bays_footprint,
                location,
                f"polygon bay side {bay_extent:.2f} m is smaller than {bays_footprint:.2f} m",
            )
            validation.require(
                row_extent + GEOMETRY_TOLERANCE_M >= rows_footprint,
                location,
                f"polygon row side {row_extent:.2f} m is smaller than {rows_footprint:.2f} m",
            )

    validation.require(set(sites) == layout_ids, "container_sites_v4.geojson", "site ids must exactly match layout")
    validation.require(set(axes) == layout_ids, "container_site_bay_axes_v4.geojson", "site ids must exactly match layout")


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
    sites = collect_sites(source_documents.get("containerSites", {}), terminal_crs, validation)
    axes = collect_axes(source_documents.get("bayAxes", {}), terminal_crs, validation)
    validate_layout(layout, sites, axes, validation)
    validation.finish()

    normalized = {
        key: canonical_json(source_documents[key])
        if key in source_documents
        else source_texts[key].replace("\r\n", "\n").rstrip() + "\n"
        for key in SOURCE_FILES
    }
    hashes = {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in normalized.items()}
    build_id = hashlib.sha256("".join(hashes[key] for key in sorted(hashes)).encode("ascii")).hexdigest()[:16]
    manifest = canonical_json(
        {
            "schemaVersion": "4.0",
            "buildId": build_id,
            "algorithm": "sha256",
            "sources": {
                key: {
                    "path": SOURCE_FILES[key].replace("\\", "/"),
                    "sha256": hashes[key],
                    "characters": len(normalized[key]),
                }
                for key in sorted(SOURCE_FILES)
            },
        }
    )

    if not check_only:
        for key, text in normalized.items():
            atomic_write(templates_dir / TEMPLATE_PATHS[key], text)
        atomic_write(templates_dir / TEMPLATE_PATHS["manifest"], manifest)

    site_count = len(layout.get("sites", [])) if isinstance(layout, dict) else 0
    action = "validated" if check_only else "synchronized"
    print(f"V4 scene {action}: buildId={build_id} sites={site_count}")


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
