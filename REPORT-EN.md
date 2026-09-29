# ACE Pro (gen 1) — third-party NFC tag support and UID reporting

**A full technical report.** Written for the community: what we wanted, what was in the way,
how we got there, what works today, and what is still open.

---

> **CORRECTION (2026-09-29):** the "two chips, one antenna per pair of slots" model below is **superseded**. On Gen 1 each slot has its own antenna/reader channel; the tunnel address uses `reader` = slot index (0→spool 1, 1→spool 3, 2→spool 2, 3→spool 4). See `REPORT-RC522-TUNNEL-EN.md`.

## 1. Result in one paragraph

The stock ACE Pro firmware only understands genuine Anycubic spools. We made an ACE Pro (gen 1)
read **ordinary third-party NFC tags** (OpenSpool-compatible and vendor-specific), pass the tag data
into the printer's slot records, and let the whole ecosystem see it — the printer screen shows material
and colour, multiACE learns the spool, and Spoolman/SpoolLink can bind it. Two independent routes were
built and verified on hardware: a **register tunnel** on the stock image (research path) and a
**community-firmware fork** (production path, currently flashed).

---

## 2. Hardware and firmware background (measured, not guessed)

* **Two reader chips**, each serving **one antenna per PAIR of slots**. The pairs are **1&3** and **2&4**
  (not 1&2 / 3&4). Verified with an alias test: writing distinct values to reader register `0x2D`
  per context showed contexts `{0,2}` and `{1,3}` are electrically the same chip.
* Because a pair shares one antenna, **two tags inside a pair are seen by the same reader**, and the
  firmware serves the *last successful read* to every slot that has no value of its own — the classic
  "borrowed UID" trap. Slot attribution therefore requires **movement** (rotate the spool), not a
  register.
* **Slot record layout** (the structure the firmware exposes over the protocol):
  164-byte records, base `0x20006518`, and inside a record
  `+0x16` = `rfid` flag, `+0x18` = magic, `+0x1A` = version, `+0x1C…` (fork) / `+0x3C` (tunnel) =
  a 14-character ASCII hex UID field, plus the parsed material fields (`sku`, brand, type, colour).
  `get_status` / `get_filament_info` read `+0x16` to decide whether to report a tag at all.
* **Raw UID buffer**: `0x2000012B` on the stock image, `0x20006224` on the community firmware.
* Firmware **register primitives** exist and are reusable (this is what makes the tunnel small):
  `read_reg 0x08019D12`, `write_reg 0x08019E1C` (their first argument is just the reader index — the
  pin table lives at `0x20005EE0`, stride 24 bytes), tag identification `0x0800A6A0`,
  antenna-driver select `0x08019D6C`.
* Dead ends we proved: the commit hook `0x08014524` is unreachable for unrecognised tags (it sits
  behind a tag-state gate); engineer mode is **compiled out** (its flag is zeroed at init and never
  set — the "FORBIDDEN" string is dead data); an ATQA-based gate (v5.1) did not restore UID writes;
  the OTA updater is ACE-2 only.

---

## 3. Route A — a register "tunnel" on the stock firmware (research path)

**Idea:** instead of guessing where the firmware would accept our UID, give the host raw access to the
reader's registers, and let the host drive the tag.

* **Carrier:** the already-accepted command `filament_recognition`, whose single integer parameter
  `index` is passed through untouched. One packed integer carries everything:
  `bit 17` = magic, `bits 16:15` = reader (0..3), `bit 14` = operation (0 read / 1 write),
  `bits 13:8` = MFRC522-style register, `bits 7:0` = data or buffer offset.
* **Patch:** one 4-byte hook in the stock image at `0x0800ED58` (a `bl`) plus a position-independent
  stub in the single zero-run cave at `0x080209B4`. Everything else is untouched — `diff` against
  stock is a handful of ranges.
* **Host driver:** a Klipper command that reads the UID as two 32-bit words from the raw buffer,
  with two safety rules we recommend keeping: accept a UID only if **two consecutive reads agree**,
  and only if the frame is a valid cascade (`0x88` first byte, both BCC bytes correct).
* **Outcome:** registers read/write reliably (VersionReg `0xA1`, CommandReg, FIFO, writes verified),
  the raw UID of a third-party tag was read, and the "in-turn" behaviour of the single antenna was
  documented. Host-driven *multi-step* radio sequences (REQA → anticollision → SELECT → page read) do
  **not** work, because the firmware's own reader task owns the chip between our steps. This is why the
  production route does the radio work in firmware.

---

## 4. Route B — community-firmware fork (production path, flashed today)

**Idea:** base on the public community firmware (which already parses third-party tags) and add exactly
one thing: write the tag's UID into the slot record so the ecosystem can identify the spool.

* **Base:** public CFW `ACE_V1.3.863_20260716.bin` (113720 bytes). Data caves there are used by data
  tables, so the stub is **appended to the image tail** (free space up to the update slot limit).
* **Patch:** **one instruction** at `0x08016B9A`: the firmware's "mark this tag as unrecognised"
  `ldrb.w r1,[r3,#0x36]` is replaced by `bl 0x08023C38`.
* **Stub (108 bytes, position independent):** reads the UID from `0x20006224`; if empty it falls back
  to the original `ldrb`; otherwise it writes 14 ASCII hex characters of the UID into `record+0x3C`,
  NUL-terminates, sets `rfid = 2` (`+0x36`), magic `0x007B` (`+0x38`), version `0x0065` (`+0x3A`) and
  returns 1.
* **Result on hardware:** the CFW's own parsers recognise third-party tags — e.g. a slot reported
  `sku: OPENSPOOL`, brand `Creality`, type `PETG`, colour `[222,53,48]`; another reported Bambu Lab
  PETG. The printer screen shows material and colour, and multiACE learns the spool. The device answers
  version **`CV1.3.863`** (leading `C` marks the community build).

---

## 5. multiACE module patches (`ace.py`)

The Klipper-side multiACE module gated the tag routine on generation and on firmware flavour:

* tag-read entry points refused gen 1: two `if not self._is_v2_idx(...)` guards removed
  (lines ≈11198 and ≈11280), replaced by an explicit bypass;
* the firmware-flavour detector `_is_open_fw_idx` only accepted a version string **ending in the letter
  `O`** (`V1.1.3O`). Our build answers `CV1.3.863`, so all four of its call sites treated the device as
  stock. The detector was extended to also accept a version **starting with `C`** — one change that
  unblocks the tag read, the tag write and the two automatic "read on spool insert" paths;
* config: `rc522: true` in `printer_data/config/extended/ace.cfg`.

Backups kept next to the module: `ace.py.bak_gen1tag`, `ace.py.bak_gen1tag2`, `ace.py.bak_openfw`.

---

## 6. Flashing procedure (and one hard warning)

1. Upload the image and the flasher to the printer — Moonraker `POST /server/files/upload`
   (`root=gcodes`), then copy to `/tmp` and verify the md5.
2. `ACE_EXT_RAW METHOD=drying_stop FORCE=1 ACE=0`
3. `ACE_EXT_FW_RELEASE ACE=0` — releases the serial port (needs the `ace_ext_raw` module that provides
   `FW_RELEASE`/`FW_RESUME`).
4. On the printer: `python3 /tmp/ace_flash.py <device-by-path> flash /tmp/<image>`
   Expect: `crc16=…`, `iap_upgrade ok`, `sent N bytes in ~18 s`.
5. `ACE_EXT_FW_RESUME ACE=0`
6. Bring the link back with `POST /printer/firmware_restart` (a klippy process restart).

> **NEVER use `/etc/init.d/S60klipper start` or `... restart`.**
> That path runs the printer's own upgrade check and **re-flashes the stock ACE firmware over your
> patch** (observed: the device silently went back to `V1.3.863` and `/tmp` was wiped, taking the
> flasher with it). Always use the Moonraker firmware restart.

---

## 7. Verification (what to check, and what "good" looks like)

| check | command | expected |
|---|---|---|
| firmware flavour | `ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0` | version starts with `C` (e.g. `CV1.3.863`) |
| tag parsed | `ACE_EXT_RAW METHOD=get_filament_info INDEX=<0..3> FORCE=1 ACE=0` | third-party tag: `sku`/`brand`/`type`/`color` filled from the tag; stock returns `sku:""` |
| tag read by host | `ACE_TAG_READ ACE=0 SLOT=<0..3> MAX_MM=600` | "tag read started", rotation of the neighbour, then a bind (may abort — see limitations) |
| UID visible | printer screen / multiACE UI | material and colour shown for the loaded slot |

---

## 8. Limitations and open items (read this before expecting magic)

* **One antenna per pair**: the two slots of a pair are never separated by electronics. Attribution
  needs movement — rotate the spool (or let the loading sequence rotate it) and watch which tag leaves
  the field. Any per-slot binding that ignores this will silently attribute a spool to the wrong slot.
* **Parser codes are not unique** (`OPENSPOOL`, Bambu colour codes). Bindings must not rely on the
  parsed code alone; that is exactly why the UID is written next to it.
* **`ACE_TAG_READ` can abort** on gen 1 with "motor command rejected" because the neighbour-slot check
  uses a V2-only call, so the routine cannot prove the neighbour is empty. A gen-1 neighbour check
  (treat as empty when the neighbour record has `sku == ""` and `rfid == 0`) is the next work item.
* **Bindings do not survive a CFW re-flash** — the slot table is reset. Re-bind after flashing.
* **No OTA path for gen 1**: the updater is ACE-2 only, so flashing goes through the IAP flasher above.

---

## 9. Files in this package

| file | what it is | md5 |
|---|---|---|
| `ACE_V1.3.863_cfw_uid.bin` | production image: public CFW + UID fork (113828 B) | `6148cfc52431fc235536c6d64b5334ef` |
| `ACE_V1.3.863_20260716.bin` | base: public community firmware, unmodified (113720 B) | `9f7b9a678a96caf98d6a08842d3ff971` |
| `ACE_V1.3.863_tunnel5.bin` | stock image + register tunnel (research route, 105652 B) | `402b4b23c420b6cbd72b89dac70a2286` |
| `ACE_V1.3.863_stock.bin` | clean stock firmware (105652 B) | `dcd04589dcadd5b4feab66d33e772531` |
| `ace_flash.py` | IAP flasher, runs **on the printer** | — |
| `артефакты_stub4.s` | stub source for the CFW fork | — |
| `артефакты_stub_tunnel5.s` | stub source for the stock tunnel | — |
| `артефакты_build_tunnel5.py` | builder for the stock tunnel image | — |
| `README-для-сообщества.md` | the same story in Russian | — |
| `REPORT-EN.md` | this report | — |

**Credits and thanks:** the community firmware authors whose work made route B possible, the
multiACE author for a module that was readable enough to reason about, and the device owner whose
insistence on "the reader must read the tag" kept the project honest.

---

## 10. How we verified (raw commands and raw answers)

All commands below were issued through Klipper / Moonraker on a live, idle printer.
The raw module replies are quoted verbatim.

**Firmware flavour — stock vs patched**

```
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0
stock   -> {"id":993,"code":0,"result":{"id":1,"slots":4,"model":"Anycubic Color Engine Pro",
            "firmware":"V1.3.863","boot_firmware":"V1.0.1","structure_version":"0"},"msg":"success"}
patched -> ... "firmware":"CV1.3.863" ...        # leading C = community build with the UID fork
```
Other checks that belong here: `curl -s http://127.0.0.1:7125/printer/objects/query?ace`
(shows `status`, `gate_status`, `device_count`, `spool_mode`, `spool_binding`) and the flasher log,
which must contain all three lines: `image /tmp/<img>: 113828 bytes, crc16=0xAE92`,
`iap_upgrade: {'code': 0, 'msg': 'success'}`, `sent 113828 bytes in 19.1s`.

**Tag parsing — the whole point of the exercise**

```
ACE_EXT_RAW METHOD=get_filament_info INDEX=0 FORCE=1 ACE=0
stock   -> {"index":0,"sku":"","brand":"","type":"","icon_type":0,"color":[0,0,0],
            "colors":[[0,0,0,0]],"rfid":1,"total":330,"current":0,"diameter":0.0}
patched -> {"index":0,"sku":"OPENSPOOL","brand":"Creality","type":"PETG","icon_type":0,
            "color":[222,53,48],"colors":[[222,53,48,255]],...}     # data came from the tag itself
another slot, patched -> {"sku":"OPENSPOOL","brand":"Bambu Lab","type":"PETG","color":[255,208,11],...}
genuine Anycubic tag   -> {"sku":"AHPESL-102","brand":"AC","type":"PETG",...}   # unchanged by the patch
```

**The register tunnel (research route)** — the answer arrives in the module's own field `v`:

```
ACE_EXT_RAW METHOD=filament_recognition INDEX=145152 FORCE=1 ACE=0   # read VersionReg of reader 0
-> {"id":...,"v":161}         # 0xA1 = a reader answered; the packed index selected reg 0x37
ACE_EXT_RAW METHOD=filament_recognition INDEX=146176 FORCE=1 ACE=0   # read the slot-status pseudo-register
-> {"id":...,"v":1}
```
Packing recap: `bit 17` magic · `bits 16:15` reader 0..3 · `bit 14` write flag · `bits 13:8` register ·
`bits 7:0` data/offset. Values `< 0x20000` are ordinary slot indices and behave exactly as before.

**Tag read driven by the module (patched, gen 1)**

```
ACE_TAG_READ ACE=0 SLOT=0 MAX_MM=600
-> {"result": "ok"}
console:
  [multiACE] tag read started (ACE 1 slot 1)
  [multiACE] rc522: rotating the neighbour spool (slot 2) out of the field
```
On our bench the routine then aborted with
`rc522: motor command rejected (slot busy?) - aborting` → `neighbour rotated 0 units - field STILL
occupied` → "the two cannot be told apart" (see §8, open item: a gen-1 neighbour check).

**Antenna pairing (why a pair cannot be split electronically)**

```
write TReloadRegL (0x2D) per reader context, then read it back from every context:
ctx 0 -> 0xA2   ctx 1 -> 0xA3   ctx 2 -> 0xA2   ctx 3 -> 0xA3
=> only TWO chips exist: {0,2} and {1,3}  ==  physical slots {1,3} and {2,4}
```

---

## Appendix A — the CFW fork patch (disassembly)

Base: `ACE_V1.3.863_20260716.bin`. Exactly one instruction is changed:

```
VA 0x08016B9A   was: ldrb.w  r1, [r3, #0x36]     ; "mark the tag as unrecognised"
                now: bl      0x08023C38          ; bytes: 0d f0 4d f8
```

The stub is appended to the image tail (position independent, 108 bytes, `0x08023C38`):

```
8023c38:  b570          push   {r4, r5, r6, lr}
8023c3a:  4d19          ldr    r5, [pc, #100]        ; -> 0x20006224 (UID buffer)
8023c3c:  682c          ldr    r4, [r5, #0]
8023c3e:  f8d5 6003     ldr.w  r6, [r5, #3]
8023c42:  4334          orrs   r4, r6
8023c44:  d021          beq.n  0x8023c8a            ; no UID -> original behaviour
8023c46:  a612          add    r6, pc, #72           ; -> 0x8023c90, table "0123456789ABCDEF"
8023c48:  f103 043c     add.w  r4, r3, #60          ; record + 0x3C  (ASCII UID field)
8023c4c:  f04f 0c00     mov.w  ip, #0
8023c50:  f815 100c     ldrb.w r1, [r5, ip]         ; 7 UID bytes, two nibbles each
...       (loop: 7 iterations, writes 14 ASCII hex characters)
8023c74:  2100          movs   r1, #0
8023c76:  7021          strb   r1, [r4, #0]          ; NUL terminate
8023c78:  2102          movs   r1, #2
8023c7a:  f883 1036     strb.w r1, [r3, #54]        ; +0x36 rfid = 2  ("a tag IS present")
8023c7e:  217b          movs   r1, #123              ; 0x7B
8023c80:  8719          strh   r1, [r3, #56]         ; +0x38 magic = 0x007B
8023c82:  2165          movs   r1, #101              ; 0x65
8023c84:  8759          strh   r1, [r3, #58]         ; +0x3A version = 0x0065
8023c86:  2101          movs   r1, #1
8023c88:  bd70          pop    {r4, r5, r6, pc}
8023c8a:  f893 1036     ldrb.w r1, [r3, #54]        ; fallback: the original instruction
8023c8e:  bd70          pop    {r4, r5, r6, pc}
8023c90:  3031323334353637 39 41 42 43 44 45 46    ; "0123456789ABCDEF"
```

Net effect of the patch: **the parsed tag is always accompanied by its UID**, so a consumer can tell
two identical-material spools apart — and the screen/multiACE get material and colour from the tag.

---

## Appendix B — the stock-image tunnel patch (research route)

Base: the unmodified stock image `ACE_V1.3.863_20250518.bin`. One hook plus one stub in the single
zero-run cave:

```
VA 0x0800ED58   bl 0x080209B4        ; inside filament_recognition, replaces the pre-read guard call
                                    ; (the stub replays the original guard on the non-tunnel path)
cave 0x080209B4 stub (in the 258-byte zero run; tail of the cave holds its literal pool):
    push {r3, lr}                   ; [sp,#12] -> the full 32-bit params.index
    movw/movt r0, #0x10000          ; bit 16 used to tell "normal call" from "tunnel call"
    cmp r0 / bhs tunnel
    normal:  uxtb r0, r3 ; bl 0x0800E834 (the stock read) ; pop {r3, pc}
    tunnel:  decode reader / direction / register / data from the packed index
             read  -> bl 0x08019D12   (firmware's own read_reg,  arg = reader index)
             write -> bl 0x08019E1C   (firmware's own write_reg, args = reader, reg, value)
             reply -> format {"id":%d,"result":{"v":%d},"code":0,"msg":"ok"} via 0x080159CC
```
Reported operations in the shipped `tunnel5` build: `0x00..0x3C` read/write a reader register,
`0x3D` read the slot-status byte, `0x3E` with the flag bit = run the firmware's own identification
and return 4 bytes of the raw tag buffer at the given offset, `0x3E` without the flag = return those
4 bytes. Host-side reads used the "two identical UIDs in a row" rule plus the cascade check
(`0x88` first byte and both BCC bytes) before accepting a UID.

**Why the tunnel did not become the production route:** the firmware's own reader task owns the chip
between host commands, so host-driven multi-step radio sequences (REQ → anticollision → SELECT → page)
lose their state. Single register reads/writes are atomic and always worked; a full tag read is not.
The community firmware already contains working parsers — so the production route just hands it the
UID instead of re-implementing the radio.

---

## Appendix C — reproducing the patch by hand (there is no builder script)

The fork image was produced without a build script, so this is the exact recipe that exists:

1. take the base `ACE_V1.3.863_20260716.bin` (md5 `9f7b9a678a96caf98d6a08842d3ff971`, 113720 bytes);
2. at file offset `0xEB9A` (VA `0x08016B9A`) replace the 4 bytes `f8 93 10 36`
   (`ldrb.w r1, [r3, #0x36]`) with `f0 0d f8 4d` (`bl 0x08023C38`);
3. append 108 bytes at the end of the image (VA `0x08023C38`): the stub code, the 16-byte table
   `0123456789ABCDEF`, and a 4-byte literal pool holding the UID buffer address `0x20006224`;
4. verify: size 113828, md5 `6148cfc52431fc235536c6d64b5334ef`, crc16 `0xAE92`.

Space left before the update-slot limit: **968 bytes**. Two notes for anyone extending the patch:

- the hook fires only on the **fallback** branch, i.e. when no parser recognised the tag. A tag that
  a parser *did* recognise keeps that parser's non-unique code (`OPENSPOOL`, Bambu material/colour),
  which is precisely why a consumer cannot match it to a spool. The intended fix is a **second hook at
  VA `0x08016B32`** (the "parser succeeded" branch) that appends `,` plus the 14 hex UID to the `sku`;
- the stub contains one dead store (`movs r1, #1` at `0x08023C84`); removing it frees 2 bytes.

Everything else in the image is byte-identical to the base: the diff is exactly these 4 changed
bytes plus the 108 appended ones.
