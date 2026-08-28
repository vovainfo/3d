# tools

## `build_topology_from_geojson.py`

Собирает локальный snapshot терминала из QGIS GeoJSON и патчит stub `d3_v3`.

```powershell
# из корня репозитория
python tools/build_topology_from_geojson.py
```

Подробная инструкция: [docs/qgis-vectorization.md](../docs/qgis-vectorization.md) — раздел **14.1. Запуск build_topology_from_geojson.py**.

Кратко:

1. Обновить GeoJSON в `data/geojson/`, включая двухточечные направляющие блоков в `block_axes.geojson`, и при необходимости `data/scene_origin.txt`.
2. Запустить команду выше.
3. Обновить конфигурацию БД в EDT, закрыть и открыть форму V3.

Первая точка каждой направляющей — угол `row=1,bay=1`, вторая — соседняя вершина в направлении роста `bay`. Сборщик отклонит пропущенные, дублирующиеся и некорректные направляющие.

## `build_scene_v4.py`

Проверяет источники сцены V4, включая здания, дороги, QGIS-слои виртуальных причалов,
кранов, железнодорожных путей и направляющей рейда, ручные реестры `data/vessels-v4.json`,
`data/anchorage-vessels.json`, `data/yard-cranes-v4.json` и `data/trains-v4.json`, заполняет расчётные значения
`sites[].expected`, приводит полигоны площадок к вычисленным размерам и
синхронизирует результат с текстовыми макетами обработки `d3_v4`.

```powershell
# только проверка
python tools/build_scene_v4.py --check

# проверка и обновление макетов
python tools/build_scene_v4.py
```

Режим `--check` не изменяет файлы и показывает планируемые корректировки.
Обычный запуск дополнительно формирует `data/berths-v4.json`, единый
`data/cranes-v4.json` и нормализованный `data/railways-v4.json`, обновляет
соответствующие макеты. В железнодорожном разделе проверяются CRS и геометрия
линий, наложение веток размещения на визуальную сеть с допуском 0,5 м, ссылки
и ID, типы грузов, цвета, TEU, выход вагонов за ветку и пересечения составов.
Вся отображаемая железная дорога должна находиться в
`geojson/railways_visual_v4.geojson`; `geojson/railway_branches_v4.geojson`
используется только как невидимая направляющая вагонов. Дороги задаются
осевыми линиями в `geojson/roads.geojson` с полями `id`, `color` и `width_m`
и копируются в макет `Roads_geojson`. Граница порта по суше — ровно одна
полилиния в `geojson/port_boundary.geojson` с теми же полями; она копируется
в макет `PortBoundary_geojson`. Примеры ручных
реестров находятся в
`docs/yard-cranes-v4.example.json`, `docs/trains-v4-example.json` и
`docs/anchorage-vessels-example.json`. У судна рейда обязательны
`plannedBerthId` (существующий причал), `plannedLoad` и `plannedUnload`
(объекты с неотрицательными целыми `ft10`, `ft20`, `ft40`).
JSON рейда — демо и fallback; при `ИспользоватьСудаИзEDS` runtime берёт суда
из `Документ.ЕДС_Рейс`. Направляющая рейда задаётся двухточечным
`geojson/anchorage.geojson`; JSON судов копируется в макет без повторной сериализации.
Скрипт не изменяет BSL-модули и не затрагивает V3. При ошибках исходники и
макеты не обновляются; манифест записывается последним и связывает исходные и
производные данные общим `buildId`.
