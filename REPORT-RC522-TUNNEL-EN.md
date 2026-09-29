# Gen 1 RC522 tunnel — design, contract, and live verification

**Status:** verified on live hardware (2026-09-29). Selects and reads third-party
NFC tags through the ACE Pro (Gen 1) reader chip from the normal command surface.

This document describes the *tunnel*: a small firmware append plus a 4-byte hook
that turns an existing firmware command into a remote control for the MFRC522
(ISO14443-A) reader. It is a debug/carrier route, not a product command surface —
the intended consumer is multiACE's own tag identification path (see *Next steps*).

---

## 1. Why a tunnel

The ACE Pro carries an RC522-family reader and antennas for the spools, driven by
the stock application. The community firmware exposes no command to talk to that
chip, so a host cannot issue REQA/anticollision/READ itself. The tunnel reuses a
command the firmware already has (`filament_recognition`, whose handler lives at
VA `0x080144D4`) and dispatches *our* operations from a packed index parameter.

## 2. Shape of the patch

| item | value |
|---|---|
| base image | `ACE_V1.3.863_20260716.bin` (OpenCubic v1.0.2 release asset), 113720 B, md5 `9f7b9a678a96caf98d6a08842d3ff971` |
| hook | VA `0x080144F2`, `f7fdfc3f` → `bl <stub>` (early in the handler) |
| stub | VA `0x08023CA4`, 804 B, appended to the parser stub at the image tail |
| image ceiling | **`0x1C000` = 114688 B** (app start `0x08008000` → IAP staging base `0x08024000`). An oversized image is silently accepted (`iap_upgrade: success`) but never commits — the size must be asserted by the builder |
| result image | `ACE_V1.3.863_tunnel_ops.bin`, 114632 B, crc16 `0x1AC9`, version `CV1.3.871`, md5 `219df3df77f7c7e1e15a79d580a2379e` |

The hook sits *before* the handler's own reader work, so a tunnel index never
triggers the stock recognition kick. Ordinary slot indices (0..3) still replay the
displaced call byte-for-byte — stock behaviour is preserved.

## 3. Host contract

Packed index (32 bit), sent **signed** — the firmware parses parameters with a
signed `strtol`, so any value ≥ 2³¹ must be transmitted as `packed - 2³²`:

```
0x80000000 | (reader << 24) | (op << 16) | ((a1 & 0x3F) << 8) | (a2 & 0xFF)
```

Replies arrive in `result.code` (the stub formats them through the firmware's own
JSON writer): `{"id":%d,"result":{"code":%d},"msg":"ok"}`.

| op | meaning | args | returns |
|---|---|---|---|
| 0 | read register | `a1` = register | register value |
| 1 | write register | `a1` = register, `a2` = value | 0 |
| 2 | FIFO write byte | `a1` = index, `a2` = byte | 0 |
| 3 | PCD command | `a1` = TX byte count, `a2` = command (0x0C TRANSCEIVE) | status |
| 4 | FIFO read byte | `a1` = index | FIFO byte |
| 5 | received bits | — | count (16 bytes → `0x80`) |
| 6 | REQA + anticollision + SELECT | — | 0 = card ACTIVE |
| 7 / 8 | acquire (hold) / release | — | saved state / 0 |

## 4. Antenna map (Gen 1 — owner-confirmed)

| `reader` | antenna | spool |
|---|---|---|
| 0 | antenna 0 | spool 1 |
| 1 | antenna 1 | spool 3 |
| 2 | antenna 2 | spool 2 |
| 3 | antenna 3 | spool 4 |

There is one reader/antenna channel per slot. The ACE 2 driver's
`reader = 1 if slot >= 2 else 0` does **not** apply to Gen 1, and the earlier
"pairs 1&3 / 2&4" guess is wrong.

## 5. What made it work (bring-up essentials)

1. **Force the antenna.** Write `TxControlReg` (`0x14`) with bits 0..1 set
   (`|= 0x03`) unconditionally; the firmware's own helper skips the write when
   those bits already read as set.
2. **Respect the host order.** `TXMODE |= 0x80` → `RXMODE |= 0x80` →
   `BitFraming = 0x00` → FIFO writes → TRANSCEIVE. The bring-up inside the tunnel
   resets `BitFraming` for REQA, so a `BitFraming = 0` written *before* it does
   not survive.
3. **Restore the recognition state on every reply** (all ops, all error paths).
   Without it the unit stays paused and multiACE reports `status=busy`
   (“Power-cycle the ACE”); with it the unit remains `ready`.
4. **Keep the tag in the field.** The antenna sees the spool tag only while it
   faces the coil; rotating the spool (~half a turn) moves the tag out of range.

## 6. Live verification

> **UID convention:** the table's *UID* column is the ISO14443-3 UID (page 0 bytes 0..2
> and 4..7; byte 3 is `BCC0` and byte 8 starts the next page). The raw 16 bytes are page 0
> exactly as read.

`select-scan` (one op-7 hold, SELECT on readers 0..3):

```
reader 0..3: SELECT code 0 -> card present
```

Page-0 read (`op7 hold → SELECT → READ(0x30, page 0) → op8 release`), `bits = 128`:

| reader | 16 bytes (page 0) | UID |
|---|---|---|
| 0 (spool 1) | `04 22 52 FC 51 C8 2A 81 32 48 00 00 E1 10 6D 00` | `04 22 52 51 C8 2A 81` |
| 1 (spool 3) | `53 42 70 E9 D1 B5 00 01 65 48 00 00 E1 10 12 00` | `53 42 70 D1 B5 00 01` |
| 2 (spool 2) | same as reader 0 | `04 22 52 51 C8 2A 81` |
| 3 (spool 4) | same as reader 1 | `53 42 70 D1 B5 00 01` |

The UIDs were confirmed independently with a phone NFC reader — the tunnel returns
the real tag content, not an artefact. The unit stays `ready` throughout; no
power-cycle is needed after a tunnel session.

## 7. Reproduce

Build the image (needs `arm-none-eabi` toolchain; the builder refuses anything
over the ceiling, and reproduces the working UID image byte-for-byte when built
without the extra hook):

```bash
python3 artefacts_build_rc522_tunnel.py ACE_V1.3.863_20260716.bin \
    ACE_V1.3.863_tunnel_ops.bin \
    --hook-fixed --hook 0x080144F2 --stub artefacts_stub_rc522.s --version 1.3.871
```

Flash and probe from the printer host (the serial device belongs to the ACE):

```bash
# flash: release the port from Klipper first, then
python3 ace_flash.py /dev/serial/by-path/... flash ACE_V1.3.863_tunnel_ops.bin
# probe (release the port, run, resume)
python3 probe/probe_ntag.py select-scan --dev /dev/serial/by-path/...
python3 probe/probe_ntag.py read-full --page 0 --dev /dev/serial/by-path/...
```

Never flash through `/etc/init.d/S60klipper start|restart` (it re-flashes the
factory image). Do not flash while the printer is printing.

## 8. Open items

* Readers 0/2 and 1/3 currently report the same UID — either two slots share one
  reader chip's RF path, or the spools carry identical tags. Worth a second
  spool swap before concluding.
* multiACE integration is the next step: expose the tunnel as the tag source for
  filament identification (`filament_identify`), so the unit identifies
  third-party spools through the community firmware rather than the vendor path.
* Regression coverage for the ordinary slot path and for the parser stub is in
  `tests/`.

## 9. Licence

Our files: GPL-3.0-or-later (see `LICENSE`). The firmware images are third-party
material — see `NOTICE.md` for provenance and scope.
