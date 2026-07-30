# tools

## `build_topology_from_geojson.py`

Собирает локальный snapshot терминала из QGIS GeoJSON и патчит stub `d3_v3`.

```powershell
# из корня репозитория
python tools/build_topology_from_geojson.py
```

Подробная инструкция: [docs/qgis-vectorization.md](../docs/qgis-vectorization.md) — раздел **14.1. Запуск build_topology_from_geojson.py**.

Кратко:

1. Обновить GeoJSON в `data/geojson/` и при необходимости `data/scene_origin.txt`.
2. Запустить команду выше.
3. Обновить конфигурацию БД в EDT, закрыть и открыть форму V3.
