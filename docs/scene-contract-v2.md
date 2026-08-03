# Контракт сцены v2 (snapshot / chunk / delta)

Оси данных: **Z-up** в JSON (`x`, `y` — план, `z` — высота основания).
В Three.js сцена **Y-up**; viewer применяет `(x, z, y)` и поворот `-rotation` вокруг Y.
Единицы: метры. Углы: градусы, против часовой стрелки в плане.

## Состояния клиента

`Idle → Loading → Live → Resync`

- `Loading` — принимаются чанки одного `snapshotId`.
- `Live` — принимаются delta с `baseSnapshotId` и монотонной `version`.
- Пропуск `version` или смена `snapshotId` → `Resync` (полный снимок заново).

## snapshot

```json
{
  "type": "snapshot",
  "snapshotId": "stub-1710000000000",
  "generatedAt": "2026-07-26T12:00:00Z",
  "units": "m",
  "site": { "width": 700, "length": 620, "gridStep": 2 },
  "topology": {
    "blocks": [
      {
        "id": "A",
        "originX": 8,
        "originY": 8,
        "rotationDeg": 0,
        "rowPitch": 2.75,
        "bayPitch": 12.5,
        "rowSize": 2.42,
        "baySize": 11.75,
        "tierPitch": 2.591,
        "rows": 25,
        "bays": 60,
        "maxTier": 7
      }
    ]
  },
  "expectedChunks": 1
}
```

`rowSize` / `baySize` задают физический размер контейнера, а `rowPitch` / `bayPitch` — расстояние между центрами соседних позиций. Поэтому pitch может быть больше size: при сборке свободное место блока равномерно распределяется между контейнерами. Если минимальная сетка контейнеров не помещается в полигон блока, snapshot не создаётся.

Текущий stub по-прежнему отдаёт legacy `version: 1` + `objects[]` целиком внутри HTML для первичной загрузки. Чанковая передача — следующий шаг транспорта.

## snapshotChunk

```json
{
  "type": "snapshotChunk",
  "snapshotId": "stub-1710000000000",
  "chunkIndex": 0,
  "blockId": "A",
  "objects": [
    {
      "id": "C-0-0-1",
      "kind": "container40",
      "name": "C-0-0-1",
      "x": 8,
      "y": 8,
      "z": 0,
      "rotation": 0,
      "color": "#1769aa",
      "blockId": "A",
      "row": 0,
      "bay": 0,
      "tier": 1,
      "status": "yard",
      "observedAt": "2026-07-26T12:00:00"
    }
  ]
}
```

Повторная передача чанка с тем же `snapshotId` + `chunkIndex` безопасна (идемпотентна).

## delta

```json
{
  "type": "delta",
  "baseSnapshotId": "stub-1710000000000",
  "version": 12,
  "moves": [
    { "id": "C-0-0-1", "x": 20.5, "y": 8, "z": 0 }
  ],
  "upserts": [],
  "removed": [],
  "quality": [
    { "code": "unknownLocation", "id": "C-9-9-9", "detail": "block X missing" }
  ]
}
```

- `moves` содержат **абсолютные** координаты → повтор той же delta идемпотентен.
- `upserts` — полные объекты (add или replace).
- `removed` — массив id.
- Пропуск версии → клиент запрашивает полный resync.

## ack (мост 1С → JS)

Команда уходит через `#scene-command-bridge`:

| `op` | Назначение |
|------|------------|
| `applyDelta` | `{ op, delta }` |
| `focus` | `{ op, id }` |
| `add` / `remove` / `move` | точечные операции (совместимость / отладка) |

Подтверждение: `data-ack == data-seq`, ошибка в `data-error`.

## Свежесть данных

`observedAt` — серверное время последнего наблюдения.
Индикатор «устарело», если возраст > 120 с относительно времени снимка/delta на стороне адаптера (не клиентских часов UI).
