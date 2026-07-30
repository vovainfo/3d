#!/usr/bin/env python3
"""Сборка site.shoreline / site.water + topology.blocks из QGIS GeoJSON."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


DEFAULT_TIER_PITCH = 2.591
MARGIN_M = 20.0


def parse_scene_origin(path: Path) -> dict:
    data: dict[str, str | float] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {"originEasting", "originNorthing", "rotationDeg"}:
            data[key] = float(value.replace(",", "."))
        else:
            data[key] = value
    required = {"originEasting", "originNorthing"}
    missing = required - set(data)
    if missing:
        raise SystemExit(f"{path}: нет полей {sorted(missing)}")
    data.setdefault("crs", "EPSG:32652")
    data.setdefault("rotationDeg", 0.0)
    return data


def load_geojson(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def prop(properties: dict, *names: str, default=None):
    for name in names:
        if name in properties:
            return properties[name]
        for key, value in properties.items():
            if key.strip() == name:
                return value
    return default


def to_local(easting: float, northing: float, origin_e: float, origin_n: float) -> tuple[float, float]:
    return easting - origin_e, northing - origin_n


def round2(value: float) -> float:
    return round(value, 2)


def qgis_azimuth_to_scene(value: float) -> float:
    """Азимут QGIS (от севера по часовой) -> угол сцены (от востока против часовой)."""
    return (90.0 - value) % 360.0


def rotate_point(x: float, y: float, scene_rotation_deg: float) -> tuple[float, float]:
    """Поворот локальных координат: направление scene_rotation становится +X."""
    theta = math.radians(-scene_rotation_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    return x * cos_t - y * sin_t, x * sin_t + y * cos_t


def normalize_deg(value: float) -> float:
    return value % 360.0


def slot_center_from_corner(
    corner_x: float,
    corner_y: float,
    rotation_deg: float,
    row_pitch: float,
    bay_pitch: float,
) -> tuple[float, float]:
    """Перевод угла слота row=1,bay=1 в его центр, ожидаемый viewer."""
    ang = math.radians(rotation_deg)
    half_bay = bay_pitch * 0.5
    half_row = row_pitch * 0.5
    return (
        corner_x + half_bay * math.cos(ang) - half_row * math.sin(ang),
        corner_y + half_bay * math.sin(ang) + half_row * math.cos(ang),
    )


def xy_list_from_coords(
    coords: list,
    origin_e: float,
    origin_n: float,
    scene_rotation_deg: float,
) -> list[dict]:
    points: list[dict] = []
    for easting, northing in coords:
        x, y = to_local(easting, northing, origin_e, origin_n)
        x, y = rotate_point(x, y, scene_rotation_deg)
        points.append({"x": round2(x), "y": round2(y)})
    return points


def collect_shoreline(
    geojson: dict,
    origin_e: float,
    origin_n: float,
    scene_rotation_deg: float,
) -> list[dict]:
    points: list[dict] = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        points.extend(
            xy_list_from_coords(
                geometry.get("coordinates") or [],
                origin_e,
                origin_n,
                scene_rotation_deg,
            )
        )
    if len(points) < 2:
        raise SystemExit("shoreline.geojson: нужно >= 2 вершин LineString")
    return points


def collect_water(
    geojson: dict | None,
    origin_e: float,
    origin_n: float,
    scene_rotation_deg: float,
) -> list[list[dict]]:
    """Возвращает список внешних колец water-полигонов."""
    if geojson is None:
        return []
    rings: list[list[dict]] = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry") or {}
        gtype = geometry.get("type")
        if gtype == "Polygon":
            coords = geometry.get("coordinates") or []
            if not coords:
                continue
            ring = xy_list_from_coords(coords[0], origin_e, origin_n, scene_rotation_deg)
            if len(ring) >= 3:
                rings.append(ring)
        elif gtype == "MultiPolygon":
            for poly in geometry.get("coordinates") or []:
                if not poly:
                    continue
                ring = xy_list_from_coords(poly[0], origin_e, origin_n, scene_rotation_deg)
                if len(ring) >= 3:
                    rings.append(ring)
    return rings


def collect_blocks(
    blocks_geojson: dict,
    origins_geojson: dict,
    origin_e: float,
    origin_n: float,
    scene_rotation_deg: float,
) -> list[dict]:
    origins: dict[str, tuple[float, float]] = {}
    for feature in origins_geojson.get("features", []):
        block_id = str(prop(feature.get("properties") or {}, "block_id", default="") or "")
        geometry = feature.get("geometry") or {}
        if not block_id or geometry.get("type") != "Point":
            continue
        easting, northing = geometry["coordinates"][:2]
        x, y = to_local(easting, northing, origin_e, origin_n)
        origins[block_id] = rotate_point(x, y, scene_rotation_deg)

    blocks: list[dict] = []
    missing: list[str] = []
    for feature in blocks_geojson.get("features", []):
        properties = feature.get("properties") or {}
        block_id = str(prop(properties, "id", default="") or "")
        if not block_id:
            continue
        if block_id not in origins:
            missing.append(block_id)
            continue
        corner_x, corner_y = origins[block_id]
        # угол в локальной CRS до поворота сцены, затем вычитаем scene rotation
        abs_rotation = qgis_azimuth_to_scene(float(prop(properties, "rotation", default=0) or 0))
        rotation_deg = normalize_deg(abs_rotation - scene_rotation_deg)
        row_pitch = float(prop(properties, "row_pitch", default=2.75) or 2.75)
        bay_pitch = float(prop(properties, "bay_pitch", default=6.25) or 6.25)
        ox, oy = slot_center_from_corner(
            corner_x,
            corner_y,
            rotation_deg,
            row_pitch,
            bay_pitch,
        )
        blocks.append(
            {
                "id": block_id,
                "originX": round2(ox),
                "originY": round2(oy),
                "rotationDeg": round2(rotation_deg),
                "rowPitch": row_pitch,
                "bayPitch": bay_pitch,
                "tierPitch": DEFAULT_TIER_PITCH,
                "rows": int(prop(properties, "rows", default=0) or 0),
                "bays": int(prop(properties, "bays", default=0) or 0),
                "maxTier": int(prop(properties, "max_tier", default=7) or 7),
            }
        )

    if missing:
        raise SystemExit(f"нет block_origins для: {', '.join(sorted(missing))}")

    extra = sorted(set(origins) - {b["id"] for b in blocks})
    if extra:
        raise SystemExit(f"лишние block_origins без yard_blocks: {', '.join(extra)}")

    blocks.sort(key=lambda item: item["id"])
    return blocks


def block_corners(block: dict) -> list[tuple[float, float]]:
    """Углы сетки блока; origin блока — центр первой ячейки."""
    ang = math.radians(block["rotationDeg"])
    cos_a = math.cos(ang)
    sin_a = math.sin(ang)
    min_x = -block["bayPitch"] * 0.5
    min_y = -block["rowPitch"] * 0.5
    max_x = (block["bays"] - 0.5) * block["bayPitch"]
    max_y = (block["rows"] - 0.5) * block["rowPitch"]
    corners_local = ((min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y))
    result = []
    for lx, ly in corners_local:
        x = block["originX"] + lx * cos_a - ly * sin_a
        y = block["originY"] + lx * sin_a + ly * cos_a
        result.append((x, y))
    return result


def shift_points(points: list[dict], shift_x: float, shift_y: float) -> list[dict]:
    return [{"x": round2(p["x"] + shift_x), "y": round2(p["y"] + shift_y)} for p in points]


def apply_nonnegative_shift(
    shoreline: list[dict],
    water_rings: list[list[dict]],
    blocks: list[dict],
    origin_e: float,
    origin_n: float,
    margin: float,
) -> tuple[list[dict], list[list[dict]], list[dict], float, float, float, float]:
    xs: list[float] = [p["x"] for p in shoreline]
    ys: list[float] = [p["y"] for p in shoreline]
    for ring in water_rings:
        for p in ring:
            xs.append(p["x"])
            ys.append(p["y"])
    for block in blocks:
        xs.append(block["originX"])
        ys.append(block["originY"])
        for x, y in block_corners(block):
            xs.append(x)
            ys.append(y)

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    shift_x = 0.0 if min_x >= margin else (margin - min_x)
    shift_y = 0.0 if min_y >= margin else (margin - min_y)

    if shift_x or shift_y:
        shoreline = shift_points(shoreline, shift_x, shift_y)
        water_rings = [shift_points(ring, shift_x, shift_y) for ring in water_rings]
        shifted_blocks = []
        for block in blocks:
            item = dict(block)
            item["originX"] = round2(block["originX"] + shift_x)
            item["originY"] = round2(block["originY"] + shift_y)
            shifted_blocks.append(item)
        blocks = shifted_blocks
        max_x += shift_x
        max_y += shift_y
        min_x += shift_x
        min_y += shift_y

    width = round2(max(max_x, min_x) + margin)
    length = round2(max(max_y, min_y) + margin)
    effective_e = round2(origin_e - shift_x)
    effective_n = round2(origin_n - shift_y)
    return shoreline, water_rings, blocks, width, length, effective_e, effective_n


def build_snapshot(data_dir: Path) -> dict:
    origin = parse_scene_origin(data_dir / "scene_origin.txt")
    origin_e = float(origin["originEasting"])
    origin_n = float(origin["originNorthing"])
    scene_rotation = float(origin.get("rotationDeg", 0) or 0)

    shoreline = collect_shoreline(
        load_geojson(data_dir / "geojson" / "shoreline.geojson"),
        origin_e,
        origin_n,
        scene_rotation,
    )
    water_path = data_dir / "geojson" / "water.geojson"
    water_geo = load_geojson(water_path) if water_path.exists() else None
    water_rings = collect_water(water_geo, origin_e, origin_n, scene_rotation)
    blocks = collect_blocks(
        load_geojson(data_dir / "geojson" / "yard_blocks.geojson"),
        load_geojson(data_dir / "geojson" / "block_origins.geojson"),
        origin_e,
        origin_n,
        scene_rotation,
    )
    shoreline, water_rings, blocks, width, length, effective_e, effective_n = apply_nonnegative_shift(
        shoreline, water_rings, blocks, origin_e, origin_n, MARGIN_M
    )

    site: dict = {
        "width": width,
        "length": length,
        "gridStep": 2,
        "quayWidth": 14,
        "shoreline": shoreline,
    }
    if water_rings:
        # одно кольцо — плоский массив точек; несколько — массив колец
        site["water"] = water_rings[0] if len(water_rings) == 1 else water_rings

    return {
        "version": 3,
        "units": "m",
        "view": "top",
        "meta": {
            "sourceCrs": origin.get("crs", "EPSG:32652"),
            "sceneOrigin": {
                "easting": origin_e,
                "northing": origin_n,
                "rotationDeg": scene_rotation,
                "note": origin.get("note", ""),
            },
            "effectiveOrigin": {
                "easting": effective_e,
                "northing": effective_n,
                "note": "после scene rotation; сдвиг, чтобы локальные x/y были >= margin",
            },
            "marginM": MARGIN_M,
        },
        "site": site,
        "topology": {"blocks": blocks},
        "placements": [],
        "objects": [],
    }


def bsl_number(value: float | int, digits: int = 2) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return text


def bsl_point_array(var_name: str, points: list[dict], indent: str = "") -> list[str]:
    lines = [f"{indent}{var_name} = Новый Массив;"]
    for point in points:
        lines.append(
            f"{indent}{var_name}.Добавить(Новый Структура(\"x,y\", {bsl_number(point['x'])}, {bsl_number(point['y'])}));"
        )
    return lines


def flatten_water(site: dict) -> list[dict] | None:
    water = site.get("water")
    if not water:
        return None
    if water and isinstance(water[0], dict) and "x" in water[0]:
        return water  # type: ignore[return-value]
    # несколько колец — берём первое для stub BSL
    return water[0]  # type: ignore[index]


def write_bsl_fragment(snapshot: dict, path: Path) -> None:
    lines = [
        "// Сгенерировано tools/build_topology_from_geojson.py — не редактировать вручную.",
        "",
    ]
    lines.extend(bsl_point_array("БереговаяЛиния", snapshot["site"]["shoreline"]))
    water = flatten_water(snapshot["site"])
    water_arg = "Неопределено"
    if water:
        lines.append("")
        lines.extend(bsl_point_array("Вода", water))
        water_arg = "Вода"
    lines.extend(
        [
            "",
            "ДанныеСцены.Вставить(\"site\", Новый Структура(",
            "\t\"width,length,gridStep,quayWidth,shoreline,water\",",
            f"\t{bsl_number(snapshot['site']['width'])}, {bsl_number(snapshot['site']['length'])}, "
            f"2, 14, БереговаяЛиния, {water_arg}));",
            "",
            "Блоки = Новый Массив;",
        ]
    )
    for block in snapshot["topology"]["blocks"]:
        lines.append(
            "Блоки.Добавить(ОписаниеБлока(\"{id}\", {ox}, {oy}, {rot}, {rp}, {bp}, {tp}, {rows}, {bays}, {mt}));".format(
                id=block["id"],
                ox=bsl_number(block["originX"]),
                oy=bsl_number(block["originY"]),
                rot=bsl_number(block["rotationDeg"]),
                rp=bsl_number(block["rowPitch"]),
                bp=bsl_number(block["bayPitch"]),
                tp=bsl_number(block["tierPitch"], digits=3),
                rows=block["rows"],
                bays=block["bays"],
                mt=block["maxTier"],
            )
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def patch_object_module(module_path: Path, snapshot: dict) -> None:
    text = module_path.read_text(encoding="utf-8")
    start = text.find("\tБереговаяЛиния = Новый Массив;")
    if start < 0:
        raise SystemExit(f"{module_path}: не найден блок БереговаяЛиния")
    end = text.find("\tДанныеСцены.Вставить(\"topology\", Новый Структура(\"blocks\", Блоки));", start)
    if end < 0:
        raise SystemExit(f"{module_path}: не найден Вставить topology")
    end = text.find("\n", end) + 1

    fragment_lines = [
        "\tБереговаяЛиния = Новый Массив;",
        "\t// Реальная береговая линия (tools/build_topology_from_geojson.py).",
    ]
    for point in snapshot["site"]["shoreline"]:
        fragment_lines.append(
            f"\tБереговаяЛиния.Добавить(Новый Структура(\"x,y\", {bsl_number(point['x'])}, {bsl_number(point['y'])}));"
        )

    water = flatten_water(snapshot["site"])
    water_arg = "Неопределено"
    if water:
        fragment_lines.append("")
        fragment_lines.append("\tВода = Новый Массив;")
        fragment_lines.append("\t// Полигон акватории (site.water).")
        for point in water:
            fragment_lines.append(
                f"\tВода.Добавить(Новый Структура(\"x,y\", {bsl_number(point['x'])}, {bsl_number(point['y'])}));"
            )
        water_arg = "Вода"

    fragment_lines.extend(
        [
            "",
            "\tДанныеСцены.Вставить(\"site\", Новый Структура(",
            "\t\t\"width,length,gridStep,quayWidth,shoreline,water\",",
            f"\t\t{bsl_number(snapshot['site']['width'])}, {bsl_number(snapshot['site']['length'])}, "
            f"2, 14, БереговаяЛиния, {water_arg}));",
            "",
            "\tБлоки = Новый Массив;",
            "\t// Реальные контейнерные блоки из QGIS GeoJSON.",
        ]
    )
    for block in snapshot["topology"]["blocks"]:
        fragment_lines.append(
            "\tБлоки.Добавить(ОписаниеБлока(\"{id}\", {ox}, {oy}, {rot}, {rp}, {bp}, {tp}, {rows}, {bays}, {mt}));".format(
                id=block["id"],
                ox=bsl_number(block["originX"]),
                oy=bsl_number(block["originY"]),
                rot=bsl_number(block["rotationDeg"]),
                rp=bsl_number(block["rowPitch"]),
                bp=bsl_number(block["bayPitch"]),
                tp=bsl_number(block["tierPitch"], digits=3),
                rows=block["rows"],
                bays=block["bays"],
                mt=block["maxTier"],
            )
        )
    fragment_lines.append('\tДанныеСцены.Вставить("topology", Новый Структура("blocks", Блоки));')
    fragment_lines.append("")

    new_text = text[:start] + "\n".join(fragment_lines) + text[end:]

    if "ДобавитьИнфраструктуруДемо(Объекты, МассивЦветов);" in new_text and "// ДобавитьИнфраструктуруДемо" not in new_text:
        new_text = new_text.replace(
            "\n\tДобавитьИнфраструктуруДемо(Объекты, МассивЦветов);",
            "\n\t// ДобавитьИнфраструктуруДемо(Объекты, МассивЦветов); // отключено: координаты под старый stub",
            1,
        )

    module_path.write_text(new_text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Путь к topology_snapshot.json (по умолчанию data/topology_snapshot.json)",
    )
    parser.add_argument(
        "--patch-bsl",
        type=Path,
        default=None,
        help="Патч ObjectModule.bsl (по умолчанию src/.../d3_v3/ObjectModule.bsl)",
    )
    parser.add_argument("--no-patch-bsl", action="store_true")
    args = parser.parse_args()

    data_dir = args.data_dir
    out_path = args.out or (data_dir / "topology_snapshot.json")
    snapshot = build_snapshot(data_dir)
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_bsl_fragment(snapshot, data_dir / "topology_snapshot.bsl.txt")

    if not args.no_patch_bsl:
        module_path = args.patch_bsl or (
            Path(__file__).resolve().parents[1]
            / "src"
            / "DataProcessors"
            / "d3_v3"
            / "ObjectModule.bsl"
        )
        patch_object_module(module_path, snapshot)

    water = snapshot["site"].get("water")
    water_n = 0
    if water:
        water_n = len(water) if water and isinstance(water[0], dict) else sum(len(r) for r in water)
    print(f"wrote {out_path}")
    print(f"blocks={len(snapshot['topology']['blocks'])} shoreline={len(snapshot['site']['shoreline'])} water_pts={water_n}")
    print(f"site={snapshot['site']['width']} x {snapshot['site']['length']} m")
    print(f"sceneRotation={snapshot['meta']['sceneOrigin']['rotationDeg']}")
    print(f"effectiveOrigin E={snapshot['meta']['effectiveOrigin']['easting']} N={snapshot['meta']['effectiveOrigin']['northing']}")


if __name__ == "__main__":
    main()
