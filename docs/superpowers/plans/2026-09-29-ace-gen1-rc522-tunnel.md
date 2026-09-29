# ACE Pro (Gen 1) — RC522 tunnel: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать Gen 1 ACE Pro тот же RC522‑туннель, что у ACE2‑Open (op 0–6 через упакованный `index`), и в финале — команду `filament_identify` + версию с хвостовой `O`, чтобы multiACE читал и писал NTAG **без единой правки** в своём коде.

**Architecture:** Отдельный стаб в хвосте образа + точечные врезки `bl`; носитель на этапе A — проверенный `filament_recognition` (упакованный `index`), на этапе B — своя команда `filament_identify` через запись в таблице команд ACE‑протокола. Хост‑правки допускаются только в отладочных скриптах, не в upstream.

**Tech Stack:** Arm Thumb‑2 (`arm-none-eabi-as/ld/objcopy`), Python 3 для сборщика и проб, pyserial для проб на живом устройстве, IAP‑флешер `ace_flash.py`, Moonraker HTTP для прогона команд на принтере.

**Spec:** `docs/superpowers/specs/2026-09-29-ace-gen1-rc522-tunnel-design.md`

## Global Constraints

- База сборки: `ACE_V1.3.863_20260716.bin`, md5 `9f7b9a678a96caf98d6a08842d3ff971`, 113720 B (ассет OpenCubic v1.0.2).
- Текущий рабочий образ: `ACE_V1.3.863_cfw_uid.bin`, md5 `6148cfc52431fc235536c6d64b5334ef`, 113828 B, crc16 `0xAE92` — его сборка обязана **воспроизводиться байт‑в‑байт**.
- Откат: `ACE_V1.3.863_stock.bin`, md5 `dcd04589dcadd5b4feab66d33e772531`, 105652 B.
- Запас образа под хвост: **≤ 968 B** сверх текущих 108 B стаба.
- Упаковка хоста: `0x80000000 | (reader<<24) | (op<<8+8) | (a1&0x3F)<<8 | (a2&0xFF)`, `reader = 1 if slot>=2 else 0`; ответ читается как `resp['result']['code'] & 0xFF`.
- Опкоды: 0 чтение регистра, 1 запись регистра, 2 FIFO‑write, 3 PCD‑команда, 4 FIFO‑read, 5 RX‑биты, 6 SELECT.
- Регистры: MFRC522 (VersionReg `0x37` → `0xA1`), пары антенн `1&3`/`2&4`, сосед = `slot xor 2`.
- НИКОГДА не использовать `/etc/init.d/S60klipper start|restart`; порт освобождать `ACE_EXT_FW_RELEASE` / `ACE_EXT_FW_RESUME`; не заливать во время печати.
- Существующая врезка парсера (`VA 0x08016B9A`, `f8 93 10 36` → `f0 0d f8 4d`) и её 108‑байтный стаб **не изменяются**.
- Лицензия добавляемых файлов — GPL‑3.0.
- Каждая заливка: сначала офлайн‑проверка (md5/crc16/размер/дизассемблер), затем заливка, затем прогон регрессий `get_info` / `get_status` / `get_filament_info` и разбора сторонней метки (`rfid = 2`).

## Review Focus

- Обычный (неупакованный) `index` в носителе должен по‑прежнему работать как номер слота — иначе ломается штатное чтение метки.
- Отсутствие карты/шум в поле: op 6 и op 5 должны давать предсказуемый неуспех, а не зависание устройства.
- Долгая туннельная операция при параллельном опросе статуса (multiACE опрашивает ACE постоянно) — таймауты не должны рвать связь.
- Питание/сброс посреди записи NTAG — карта должна остаться читаемой (частичной записи быть не должно).
- Прошивка после врезки обязана оставаться загружаемой; строка версии и `get_info` отвечают как прежде.

---

### Task 1: Сборщик туннеля, воспроизводящий текущий образ

**Files:**
- Create: `artefacts_build_rc522_tunnel.py`
- Test: `tests/test_build_rc522_tunnel.py`

**Interfaces:**
- Consumes: `ACE_V1.3.863_20260716.bin` (base), `artefacts_stub4.s` (существующий стаб парсера).
- Produces: `build(base_path, out_path, *, hook_va=None, stub_src=None, version=None, append=b'') -> dict` с ключами `md5`, `size`, `crc16`, `diff_ranges`; CLI `python3 artefacts_build_rc522_tunnel.py <base> <out> [--hook-fixed] [--version CV1.3.863]`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_rebuild_reproduces_current_image(tmp_path):
    out = tmp_path / "rebuild.bin"
    res = build("ACE_V1.3.863_20260716.bin", str(out), hook_fixed=True,
                stub_src="artefacts_stub4.s", version="1.3.863")
    assert res["md5"] == "6148cfc52431fc235536c6d64b5334ef"
    assert res["size"] == 113828
    assert res["crc16"] == 0xAE92
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `python3 -m pytest tests/test_build_rc522_tunnel.py -v`
Expected: FAIL (`ModuleNotFoundError`/`build` отсутствует)

- [ ] **Step 3: Реализовать сборщик**: проверка md5 базы, ассемблирование стаба (`arm-none-eabi-as`/`ld`/`objcopy`, `-mcpu=cortex-m4 -mthumb`), врезка 4 байт по `0xEB9A`, дописывание стаба в хвост, вывод md5/размера/crc16 и диапазонов отличий. Значения врезки и версии — ровно как в Global Constraints.

- [ ] **Step 4: Запустить тест — должен пройти**

Run: `python3 -m pytest tests/test_build_rc522_tunnel.py -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add artefacts_build_rc522_tunnel.py tests/test_build_rc522_tunnel.py
git commit -m "feat(tunnel): builder reproducing the current image byte-for-byte"
```

---

### Task 2 (проба P1): точка врезки в обработчике `filament_recognition`

**Files:**
- Create: `probe/probe_tunnel.py` (хост‑проба через pyserial, использует кодек кадров из `ace_flash.py`)
- Create: `artefacts_stub_probe.s` (стаб‑заглушка: возвращает VersionReg)
- Modify: `artefacts_build_rc522_tunnel.py` (флаги `--hook <VA> --stub <file>`)

**Interfaces:**
- Consumes: builder из Task 1; `read_reg 0x08019D12` (примитив прошивки).
- Produces: Python‑API `TunnelProbe(dev, idx=0)` с `send(op, a1=0, a2=0, reader=0) -> Reply(field, value, raw)` и CLI поверх него: `probe/probe_tunnel.py probe --dev <path> --packed <uint32>`.

- [ ] **Step 1: Написать падающий офлайн‑тест врезки**

```python
def test_probe_hook_bytes(tmp_path):
    res = build("ACE_V1.3.863_20260716.bin", str(tmp_path/"p.bin"),
                hook_va=0x08016B9A, hook_bytes=None)   # hook_bytes вычислить из назначения
    assert res["hook_original"] == "f8931036"
```

- [ ] **Step 2: Прогнать — упасть**; Run: `pytest -q`; Expected: FAIL

- [ ] **Step 3: Найти в дизассемблере место в обработчике `filament_recognition` (таблица VA `0x080146xx`, обработчик — слово перед именем), где `index` уже разобран и регистр не используется дальше; записать VA и исходные 4 байта в `docs/notes-p1-hook.md` (VA, байты, окружение, почему безопасно).**

- [ ] **Step 4: Собрать образ с пробной врезкой и стабом VersionReg, проверить офлайн** (md5 поменялся ожидаемо, врезка на месте по дизассемблеру).

- [ ] **Step 5: Залить на устройство и проверить на живом железе**

Run (на принтере, порт освобождён):
`python3 /tmp/ace_flash.py <dev> flash /tmp/<probe-image>`; затем
`ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=<packed op=0 a1=0x37>`, затем `ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2`
Expected: первый ответ содержит `0xA1`, второй ведёт себя как обычный индекс слота (`2`) — то есть обычные значения не перехвачены.

- [ ] **Step 6: Коммит** (`artefacts_stub_probe.s`, `probe/probe_tunnel.py`, builder, `docs/notes-p1-hook.md`).

---

### Task 3 (проба P2): канал ответа

**Files:**
- Modify: `artefacts_stub_probe.s`, `probe/probe_tunnel.py`
- Create: `docs/notes-p2-reply.md`

**Interfaces:**
- Consumes: P1 (точка врезки, формат запроса).
- Produces: зафиксированное имя поля ответа и способ положить туда байт (`deliver_value(packed, value)` в стабе).

- [ ] **Step 1: Проба на живом устройстве:** отправить 8 разных значений (0x00, 0x01, 0x7F, 0x80, 0xA1, 0xFE, 0xFF, произвольное) и для каждого найти, в каком поле ответа оно появилось (полный дамп ответа в лог).

- [ ] **Step 2: Записать результат** в `docs/notes-p2-reply.md`: имя поля (или вывод «значение не доходит, канал строить самим»), ограничения (ширина, экранирование).

- [ ] **Step 3: Приёмка (тест‑шаг):**

```python
def test_value_roundtrip_field(probe):
    assert probe.send(op=0, a1=0x37).field == "code"   # или фактическое поле из P2
    assert probe.send(op=0, a1=0x37).value == 0xA1
```

- [ ] **Step 4: Коммит.**

---

### Task 4: op 0/1 в упаковке хоста (полная совместимость)

**Files:**
- Modify: `artefacts_stub_probe.s` → переименовать в `artefacts_stub_rc522.s`; builder: флаг `--packing host`
- Test: `tests/test_packing.py`

**Interfaces:**
- Consumes: P1/P2.
- Produces: `unpack(packed) -> (reader, op, a1, a2)` и реализация op 0/1 в стабе ровно по контракту хоста.

- [ ] **Step 1: Падающий тест упаковки/распаковки**

```python
def test_unpack_host_contract():
    assert unpack(0x80000000 | (1 << 24) | (0 << 16) | (0x37 << 8) | 0) == (1, 0, 0x37, 0)
```

- [ ] **Step 2: FAIL → Step 3: реализовать op 0/1 (регистр читается `read_reg`, пишется `write_reg`) → Step 4: PASS.**

- [ ] **Step 5: Живая проверка:** хост‑проба в форме вызова `_rc`: `op=0,a1=0x37` → `0xA1`; `op=1,a1=0x0D,a2=0x00` затем `op=0,a1=0x0D` → `0x00`.

- [ ] **Step 6: Регрессия:** обычный `INDEX=2` в носителе работает как индекс слота; `get_filament_info` на сторонней метке по‑прежнему даёт `sku`.

- [ ] **Step 7: Коммит.**

---

### Task 5: op 2–6 (FIFO, PCD‑команда, RX‑биты, SELECT)

**Files:**
- Modify: `artefacts_stub_rc522.s`, `tests/test_packing.py`
- Create: `probe/probe_ntag.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `probe_ntag.py read-page --page N` → 16 байт страницы; `probe_ntag.py select` → 0 при карте в поле.

- [ ] **Step 1: Живой тест‑скрипт (сначала «падает»):** `probe_ntag.py select` ожидает `0`, фактически пока ошибка/no reply.

- [ ] **Step 2: Реализовать op 2 (FIFO write), op 3 (PCD command: `0x0C`), op 5 (RX‑биты: `0x80` = полный кадр), op 6 (SELECT: `0x93 0x20` → TRANSCEIVE → статус).**

- [ ] **Step 3: Приёмка:** `READ (0x30 page)` даёт 16 байт, совпадающие с чтением той же метки телефоном (страницы 0–3 NTAG).

- [ ] **Step 4: Приёмка SELECT:** повторный `select` при пустом поле даёт ненулевой статус, устройство отвечает и не виснет (таймаут 2 с).

- [ ] **Step 5: Коммит.**

---

### Task 6: `ACE_TAG_READ` из multiACE (этап A целиком)

**Files:**
- Create: `docs/notes-a-debug-host-patch.diff` (однострочная отладочная правка носителя/поля в локальной копии `ace_rc522.py`)
- Create: `docs/evidence/tag-read-gen1.md`

**Interfaces:**
- Consumes: Task 5, живой принтер, multiACE.
- Produces: лог успешного `ACE_TAG_READ` на Gen 1.

- [ ] **Step 1: Внести отладочную правку только в копию на принтере** (`ace_rc522.py`: носитель `filament_recognition` и, если нужно, поле ответа из P2), зафиксировать дифф в репозитории.

- [ ] **Step 2: Прогнать `ACE_TAG_READ ACE=0 SLOT=0 MAX_MM=600`** и убедиться: устойчивый UID (два совпадения подряд), в логе — «tag centred»/«reading».

- [ ] **Step 3: Записать evidence в `docs/evidence/tag-read-gen1.md`** (команда, вывод, UID, версия прошивки `CV1.3.863`).

- [ ] **Step 4: Параллельная нагрузка:** во время серии из 20 туннельных чтений страницы подряд держать обычный опрос статуса (multiACE опрашивает ACE постоянно) и убедиться, что связь не рвётся, а ошибок в логе `klippy` про таймауты ACE нет.

- [ ] **Step 5: Коммит.**

---

### Task 7 (проба P4): команда `filament_identify` в образе

**Files:**
- Modify: `artefacts_build_rc522_tunnel.py` (флаг `--add-command filament_identify`), `artefacts_stub_rc522.s`
- Create: `docs/notes-p4-command.md`

**Interfaces:**
- Consumes: Task 5, таблица команд `0x080146xx`.
- Produces: запись команды (перенос таблицы в хвост с правкой указателя **или** обход таблицы), обработчик и JSON‑обвязка `index` → ответ с `code`/`id`.

- [ ] **Step 1: Проба: определить, что нужно для ответа** — снять с лога формат ответа на существующую команду (`{"id":N,"result":{...},"code":0,"msg":...}`) и зафиксировать в `docs/notes-p4-command.md` адрес форматирования и соглашение вызова.

- [ ] **Step 2: Реализовать команду** (имя строкой в хвосте, доступ из диспетчера, разбор `index`, ответ с `code`).

- [ ] **Step 3: Приёмка на живом устройстве:** `ACE_EXT_RAW METHOD=filament_identify FORCE=1 ACE=0 INDEX=<packed op=0 a1=0x37>` → ответ `code = 0xA1` (не `InvalidCommand`).

- [ ] **Step 4: Регрессия:** неизвестная команда по‑прежнему даёт `InvalidCommand`; `filament_recognition` работает как раньше.

- [ ] **Step 5: Коммит.**

---

### Task 8 (проба P5): признак туннеля и работа из панели без host‑правок

**Files:**
- Modify: `artefacts_build_rc522_tunnel.py` (флаг `--version CV1.3.863O`)
- Create: `docs/evidence/tag-read-write-panel.md`

**Interfaces:**
- Consumes: Task 7.
- Produces: версия `CV1.3.863O` в `get_info`; `rc522: true` в статусе multiACE; доступные вкладки чтения/записи.

- [ ] **Step 1: Собрать образ с версией `CV1.3.863O`, офлайн‑проверка, заливка.**

- [ ] **Step 2: Приёмка:** `/api/state` → `rc522: true`, `open_fw: true`; в панели доступны чтение и запись метки.

- [ ] **Step 3: Приёмка:** `ACE_TAG_READ` и `ACE_TAG_WRITE` из панели на живом NTAG (запись OpenSpool, обратное чтение совпадает).

- [ ] **Step 4: Прерванная запись:** вынуть метку из поля посреди записи (и отдельно — попытка записи в защищённую страницу) и убедиться, что карта остаётся читаемой всеми страницами, а устройство продолжает отвечать без перезагрузки.

- [ ] **Step 5: Evidence + коммит.**

---

### Task 9: Регрессии, отчёт и материал для PR

**Files:**
- Modify: `README-RU.md`, `REPORT-EN.md`, `docs/evidence/*`
- Create: `docs/pr-material-multiace.md`

**Interfaces:**
- Consumes: Tasks 1–8.
- Produces: обновлённые доки, md5 финального образа, описание для PR (что принимается upstream и почему теперь правок у мейнтейнера не требуется).

- [ ] **Step 1: Регрессии на живом устройстве:** сторонняя метка по‑прежнему `rfid = 2` и `sku` из CFW‑парсера; `get_info`/`get_status`/`get_filament_info` отвечают; смена слотов и сушка работают.

- [ ] **Step 2: Зафиксировать финальные md5/crc16 и запас образа; записать в отчёт.**

- [ ] **Step 3: Написать `docs/pr-material-multiace.md`** (для PR/message мейнтейнеру: что даёт туннель, почему гейт по `O` теперь честный, что именно проверено на живом NTAG).

- [ ] **Step 4: Коммит и пуш.**
