---
name: Mooring right left bow
overview: Заменить `starboard`/`port` на `right`/`left`/`bow`/`stern` в контракте судов, пересчитать размещение корпуса и обновить валидацию, сцену 1С, карточку и документацию.
todos:
  - id: mooring-contract
    content: Обновить vessels-v4.json и валидацию mooringSide на right/left/bow/stern
    status: pending
  - id: mooring-geometry
    content: Пересчитать размещение судна в ObjectModule.bsl для всех четырёх режимов
    status: pending
  - id: mooring-ui-docs
    content: Обновить карточку формы и документацию
    status: pending
  - id: mooring-verify
    content: Собрать сцену и проверить валидацию/линтеры
    status: pending
isProject: true
---

# Ориентация швартовки: right/left/bow/stern

## Контракт
В [data/vessels-v4.json](data/vessels-v4.json) поле `mooringSide` принимает только:
- `right` — правый борт к причалу, судно вдоль линии (бывший `starboard`);
- `left` — левый борт к причалу, судно вдоль линии (бывший `port`);
- `bow` — нос к причалу, судно перпендикулярно линии;
- `stern` — корма к причалу, судно перпендикулярно линии.

Существующие примеры обновить: Polar Star → `right`, FESCO Star → `left`. Старые значения `starboard`/`port` не принимать.

Семантика оси QGIS не меняется: вторая точка направляющей указывает на нос при швартовке **правым бортом**.

## Геометрия в 1С
В [src/DataProcessors/d3_v4/ObjectModule.bsl](src/DataProcessors/d3_v4/ObjectModule.bsl) функция `СобратьСуда`:

Опорные векторы из оси причала `d=(headingX, headingY)`:
- нормаль к правому борту `r=(headingY, -headingX)` (как сейчас);
- смещение в воду всегда `P - r × offset`.

| mooringSide | offset | rotation |
|-------------|--------|----------|
| `right` | `beamM/2 + clearanceM` | угол оси |
| `left` | `beamM/2 + clearanceM` | угол оси + 180° |
| `bow` | `lengthM/2 + clearanceM` | угол оси − 90° |
| `stern` | `lengthM/2 + clearanceM` | угол оси + 90° |

Логика: нос иконки судна в viewer смотрит в локальный +X. При `bow` нос направлен к причалу (против нормали воды), при `stern` — корма к причалу.

В объект сцены по-прежнему писать `mooringSide` с новым значением.

## Валидация сборщика
В [tools/build_scene_v4.py](tools/build_scene_v4.py) в `validate_vessels` заменить допустимые значения на `("right", "left", "bow", "stern")` и обновить текст ошибки.

## Карточка формы
В [src/DataProcessors/d3_v4/Forms/Форма/Module.bsl](src/DataProcessors/d3_v4/Forms/Форма/Module.bsl) в карточке судна показывать:
- `right` → `правый борт`;
- `left` → `левый борт`;
- `bow` → `носом`;
- `stern` → `кормой`.

## Документация
Обновить:
- [docs/terminal-layout-v4.md](docs/terminal-layout-v4.md) — список значений `mooringSide` и формулы смещения/угла для всех четырёх режимов;
- [docs/qgis-vectorization.md](docs/qgis-vectorization.md) — формулировку «правым бортом» оставить как опорное направление оси; кратко указать, что ориентация судна задаётся в `vessels-v4.json` через `right`/`left`/`bow`/`stern`;
- [tools/README.md](tools/README.md) — только если там явно упомянуты `starboard`/`port`.

## Проверка
1. Обновить `data/vessels-v4.json` и синхронизировать макет `VesselsV4_json` через `python tools/build_scene_v4.py`.
2. `--check` должен проходить.
3. Прогнать валидатор на отвержение `starboard`/`port` и принятие четырёх новых значений.
4. Линтеры по изменённым файлам.
