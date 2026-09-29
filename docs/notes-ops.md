<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 ACE-UID contributors -->

# Tasks 4+5 — the RC522 tunnel op set 0..8 (`artefacts_stub_rc522.s`)

Carrier: the `filament_recognition` ACE command, with the packed integer
`params.index` (hook VA `0x080144F2`, handler `0x080144D4`, index at handler
`[sp,#12]`).  The stub replies through the CFW formatter `0x0801DC74` with
`{"id":%d,"result":{"code":%d},"msg":"ok"}`; the host reads
`int(resp['result']['code']) & 0xFF`.

Image: `ACE_V1.3.863_tunnel_ops.bin` (version `CV1.3.871`, fix round 6).

```
md5     219df3df77f7c7e1e15a79d580a2379e
size    114632
crc16   0x1AC9
parser hook 0x08016B9A  f8931036 -> 0df04df8   (frozen, unchanged)
hook        0x080144F2  f7fdfc3f -> 0ff0d7fb   (bl 0x08023CA4; file fdf73ffc -> fbd7f00f)
version     0x08020AE7  36 37 -> 37 31         "CV1.3.863" -> "CV1.3.871"
stub        804 B at VA 0x08023CA4 (tail 912 B = 108 B parser stub + 804 B)
changed vs base (922 B): 0x080144F2..0x080144F5, 0x08016B9A..0x08016B9D,
             0x08020AE7..0x08020AE8, 0x08023C38..0x08023FC7
```

Build:

```
python3 artefacts_build_rc522_tunnel.py ACE_V1.3.863_20260716.bin /tmp/tunnel_ops.bin \
    --hook-fixed --hook 0x080144F2 --stub artefacts_stub_rc522.s --version 1.3.871
```

---

## 0b. Status — device acceptance CLOSED (2026-09-29)

`CV1.3.871` is the **verified reference and the fallback** (ruling R18):

* `select-scan` returns `SELECT=0` on **all four readers**;
* page-0 reads are full 16-byte frames (`bits=0x80`) and their UIDs match an
  **independent phone read** of the tags (ruling R19 also confirmed
  `reader = physical coil`: reader 0 = coil 1, reader 1 = coil 3);
* the root cause of the earlier per-reader failures was the **antenna path**,
  not the RX extraction: the unconditional `TxControlReg |= 3` write (round 4),
  the per-slot line selection (round 6) and the host register ordering
  (TxModeReg -> RxModeReg -> BitFraming) are what made the exchange work.  A
  tag parked at a channel is now read reliably, and the ACE stays `ready`.

A round-7 attempt that replaced the working firmware SELECT cascade
(`0x0800E314`) with its own REQA/anticollision/SELECT pipeline, added a
15-byte `SCAN RECORD` in the RC522 FIFO and a retry loop was built and tested
offline, then **reverted** on this ruling: the verified RX path, antenna
bring-up and op contract stay exactly as in `CV1.3.871`.  That work (and the
record-aware `select-scan`) is preserved in git history, commit `3d598a3`,
should a future image want additive per-stage diagnostics.

Any later version (`CV1.3.872`+) must be re-accepted on a live unit before it
replaces the reference.

---

## 0. Fix history

### Round 6 — per-slot selection re-derived; every reply restores the state

Owner-confirmed Gen 1 fact (supersedes the old 2-chip model): there is one
reader/antenna channel per slot, `reader` = slot index 0..3, and the physical
spools are `0 -> spool 1, 1 -> spool 3, 2 -> spool 2, 3 -> spool 4` (the
authoritative map is at the end of this file).  The live probes then showed
op 6 failing on channels where the stock recognition reads tags.  Three
changes in this round:

1. **Selection re-derived and documented** from the recognition dispatcher
   `0x08015C94` -> state-11 handler `0x080159E4` -> `0x08015A52`/`0x08015A5C`
   (`0x0800E228(4, 0)` + `0x0800E228(slot, 1)`) -> reader bring-up
   `0x0800E314` (state 6 calls it at `0x08015B56` with the same index).  The
   exact line/mask map is in §6; the stub already drives that sequence
   (`select_reader`) and now documents the addresses.
2. **The antenna write mirrors `0x0800EAEC` exactly**: `TxControlReg |= 3`
   (read-or-write, `set_bits`) instead of `= 3`.  Other register bits are
   preserved; the write stays unconditional (no early return).  If the
   select-only bring-up fails, op 6 retries once through the firmware's full
   mode-1 entry `0x0800E314(reader, 1)` -- the sequence the stock recognition
   uses on a channel it can read.
3. **Every reply path restores the recognition state captured at entry**
   (success, 255 failures, unknown op, marker).  The unknown-op and marker
   branches used to skip the restore, which could leave the machine paused
   (`status=busy` until a power cycle).  op 7 keeps the explicit hold by
   replying 0; op 8 replies `a2`.

The stub shrank 824 -> 804 B and the image 114652 -> 114632 B (56 B headroom).

### Round 2 — the hook moved early

The previous image hooked the handler at `0x0801450A`, the **reply
convergence point**.  The `filament_recognition` handler does **not** touch the
RC522; its tail is a state/slot handshake that *kicks* the reader state machine,
then replies:

```
080144F2  bl 0x08011D74        ; require *(0x200071EB) == 2 else FORBIDDEN
080144FE  cmp r3, #3           ; state == 3 (RFID disabled) -> FORBIDDEN
08014512  ldrb.w r7,[sp,#12]   ; slot = index & 0xFF
08014518  bl 0x08011D88        ; state handshake
0801451E  bl 0x08011ED4        ; store the active slot
08014522  strb #2, [state]     ; <<< kick the stock recognition task
0801450A  bl 0x0801DC74        ; reply (what the old hook replaced)
0801450E  add sp,#0x14; pop {r4,r5,r6,r7,pc}
```

The hook now replaces the handler's **first** call, `0x080144F2`
(`bl 0x08011D74`).  For a tunnel/marker index the stub replies and returns
straight to the handler epilogue `0x0801450E`, so `0x080144F2..0x08014528`
never runs.  For ordinary slot indices the stub **tail-calls** the displaced
`bl 0x08011D74` (`bx` with `lr = 0x080144F7`), so stock behaviour is
byte-for-byte identical.

### Round 3 — reader enable, ownership, FIFO path

Live device results with the round-2 image (CV1.3.867) confirmed the hook and
the register layer: reader 0 **and** reader 1 both read VersionReg `0xA1`, and
BitFraming `0x0D`/TxMode `0x12` write-then-read-back held.  The card exchange
still failed:

| op | call | round-2 answer |
|---|---|---|
| 6 | SELECT | `255` (0xFF) |
| 3 | TRANSCEIVE (2 bytes, 0x0C) | no response captured |
| 5 | RX bits | `1` |
| 4 | 18 × FIFO read | constant `0x19` |

Round 3 addresses this in three ways (details in §3/§4/§6):

1. **Reader enable.**  The firmware enables a reader with
   `0x0800E228(reader, 1)` before `0x0800E314(reader, 1)`; op 6 now does both,
   and retries once through the full `0x0800F030()` reader GPIO/antenna init on
   failure.
2. **Reader ownership.**  The recognition state machine runs in the main app
   task (`0x080137AA` -> `0x08015C94` every ~100 ms) and soft-resets / flushes /
   transceives on the same bit-banged SPI.  There is no firmware lock to take
   (the firmware never needed one).  New **op 7 (acquire)** captures the state
   byte `0x200071EC`, drives it to 0 (idle) and delays; every tunnel op 0..6
   re-asserts idle; **op 8 (release)** restores the saved value.  (Round 6
   changed the per-op part: every op captures the state at entry and restores
   it on every reply path — see §3.)
3. **FIFO path.**  The constant `0x19` is the signature of an *empty/locked*
   FIFO read, not RF data (a real FIFO read advances the pointer on every
   access, so 18 identical bytes cannot be FIFO contents).  With the reader held
   by op 7, the self-test `op 2 = 0x5A` → `op 4` must return `0x5A`;
   `tools`/`probe_tunnel.py fifo-test` runs exactly that.

### Round 4 — the transmitter was never switched on

CV1.3.868 moved the FIFO/reader-ownership problem out of the way (the FIFO
self-test now passes) but the card still never answered: SELECT `255`, op 3
status 0, op 5 bits 0, and op 4 constant.  The reader chip is reachable and its
FIFO works, so the only remaining layer is the **RF transmitter**.

The firmware switches the antenna on in `0x0800EAEC(reader, 3)`, which is a
read-modify-write of `TxControlReg` (0x14) that **returns early without writing
when bits 0..1 already read as set** (`cmp r4, r0; beq 0x0800EB2A`).  op 6
previously relied on that routine (through `0x0800E314(reader,1)`), so if the
`0x14` readback is stale/misleading the write is skipped and the field never
comes up.  op 6 now performs the bring-up itself and writes `TxControlReg`
**unconditionally** (see §4/§6).

### Round 5 — the host frame is 2 bytes with hardware CRC

Live evidence (image CV1.3.869): `op 6` SELECT returned **0** with the tag
parked at the antenna, but the host-driven READ right after it returned
nothing (`op 2` FIFO writes 0, `op 3` status 0, `op 5` RX bits **0**,
`op 4` all `0x00`).

Root cause: the multiACE host (`ace_rc522.py`, `_rc_setup_crc` +
`_rc_read_page`) drives a page read exactly like the stock firmware's own
read loop (`0x08015B50` → `0x0800E314(reader,1)` → `0x0800E3E0`):

```
op 0/1  TXMODE  0x12 |= 0x80      (TxCRCEn = 1)
op 0/1  RXMODE  0x13 |= 0x80      (RxCRCEn = 1)
op 1    BITFRAMING 0x0D = 0x00
op 2    0x30, <page>              (a bare 2-byte frame, NO software CRC)
op 3    TRANSCEIVE a1=2, a2=0x0C
op 5    RX bits -> 0x80 (16 bytes)
op 4    x16 FIFO reads
```

The firmware's read (`0x0800E3E0`) does the same: it sets TxCRCEn and RxCRCEn
and transmits a **2-byte** frame; the RC522 appends the CRC (and, with
RxCRCEn, verifies and strips the tag's reply CRC before it reaches the
FIFO).  Round 3 had op 3 *clear* TxCRCEn ("the host frame already carries
its CRC"), which silently dropped the CRC from the host's frame — the tag
never answered and the FIFO stayed empty.  That is exactly the observed
failure signature.

Round-5 changes in `artefacts_stub_rc522.s`:

1. **op 3 sets TxCRCEn** (`set_bits(0x12, 0x80)`) and RxCRCEn
   (`set_bits(0x13, 0x80)`), mirroring `0x0800E3E0`/`0x0800EB8C`; the
   bounded ComIrq wait is the firmware's own 0xBB8 iterations.
2. **op 3 re-asserts the antenna bits** (`set_bits(0x14, 0x03)`) and the
   slot selection right before the transceive — no reset, so a pre-loaded
   FIFO frame survives, but op 3 no longer *assumes* a prior op 6.
3. **op 1 on TxModeReg (0x12) runs `full_bringup` first** — the host's first
   register write of a transceive sequence (`_rc_setup_crc`) is where the
   reader is brought up now, i.e. **before** the host writes its FIFO frame.
   Any chip reset/init therefore happens before the frame exists, never
   after it.  `full_bringup` is the op-6 recipe factored out, so op 6
   behaviour is unchanged.
4. `probe/probe_ntag.py` pushes the same bare 2-byte frame (the `--raw`
   option is gone: with hardware CRC the CRC bytes are not in the FIFO).

The host's 2-byte frame + hardware CRC + CRC strip gives `op 5 = 0x80` and
16 bytes of page data — the acceptance shape.  If the device still fails,
§7.6 lists the exact per-step register reads and their interpretation.

## 1. Packed index and branching

```
index = 0x80000000 | (reader<<24) | (op & 0xFF)<<16 | ((a1 & 0x3F)<<8) | (a2 & 0xFF)
```

The firmware parses `params.index` with a **signed** `strtol`, so the host must
send `packed - 2**32` when bit 31 is set (R10, `probe_tunnel.as_signed32`).

The stub branches:

| index | behaviour |
|---|---|
| bit 31 set | op 0..8 dispatch, then handler epilogue (early exit) |
| `== 0x7FFFFFFF` | lossy unsigned-host fallback → fixed **op0 / reader0 / a1=0x37** |
| `>= 0x10000`, bit 31 clear | MARKER (code 90 with msg `"ok"`, reachability only) |
| `0..3` / anything else | tail-call `bl 0x08011D74` (stock handler, unchanged) |

## 2. Register map used

| reg | name | used by |
|---|---|---|
| 0x01 | CommandReg | op 3 (0x00 Idle, 0x0C TRANSCEIVE) |
| 0x02 | ComIEnReg | op 3 (0xF7, as firmware transceive) |
| 0x04 | ComIrqReg | op 3 (clear with 0x7F, wait for 0x31 = RxIRq\|IdleIRq\|TimerIRq) |
| 0x06 | ErrorReg | op 3 status (0 = OK) |
| 0x09 | FIFODataReg | op 2/4, ISO14443A frames |
| 0x0A | FIFOLevelReg | flush (write 0x80), op 5 level (read & 0x7F) |
| 0x0C | ControlReg | op 5 last-bits (read & 0x07) |
| 0x0D | BitFramingReg | op 3 last-byte bits + StartSend (0x80) |
| 0x12 | TxModeReg | op 3 sets TxCRCEn (reader appends the CRC); op 1 write here triggers `full_bringup` |
| 0x13 | RxModeReg | op 3 sets RxCRCEn/check+strip the tag CRC |
| 0x14 | TxControlReg | op 3 `|= 0x03`, op 6 `|= 0x03` (antenna bits, read-or-write, always) |
| 0x2A | TModeReg | op 3 sets TAuto (0x80); also programmed by the timer |
| 0x37 | VersionReg | op 0 probe (0xA1) |

## 3. Op semantics (CFW primitives)

| op | arguments | implementation | `result.code` |
|---|---|---|---|
| 0 | `a1` = reg | `read_reg(reader, a1)` (`0x0800E734`) | register value |
| 1 | `a1` = reg, `a2` = value | `write_reg(reader, a1, a2)` (`0x0800E7C0`); a write to **0x12 (TxModeReg)** runs `full_bringup` first (§4) | 0 |
| 2 | `a1` = index, `a2` = byte | `write_reg(reader, 0x09, a2)` (FIFO auto-increments) | 0 |
| 3 | `a1` = TX bytes, `a2` = command | `xcv_core` (see below) | ErrorReg |
| 4 | `a1` = index | `read_reg(reader, 0x09)` | FIFO byte |
| 5 | `a1` = 0 | `rx_bits` (see §5) | received bits |
| 6 | — | full reader bring-up + `TxControlReg |= 0x03` + SELECT (see §4) | 0 = card present |
| 7 | — | acquire: save state, drive it to 0, drain (`osDelay(200)`) | the saved state byte |
| 8 | `a2` = state | release: restore `0x200071EC` from `a2` | 0 |

**State restore (round 6).**  Every op captures the recognition state
(`0x200071EC`) at entry, drives it to 0 (`pause_machine`) and — on **every**
reply path, including a 255 failure, an unknown op and the marker — puts the
captured value back.  The unknown-op and marker branches used to skip that
restore; a skipped restore could leave the machine paused and the ACE in
`status=busy` until a power cycle.  op 7 is the explicit hold: its reply
restores 0, so the machine stays paused.  op 8 restores `a2` (usually the
value op 7 returned) and then restores it again through the shared reply path.
`op 3` (`xcv_core`) mirrors the firmware transceive `0x0800EB8C` for command
`0x0C`:

1. re-assert the slot selection and `set_bits(reader, 0x14, 0x03)` — antenna
   bits on (no reset, so a host-preloaded FIFO frame survives);
2. `set_bits(reader, 0x12, 0x80)` — TxCRCEn on: the RC522 appends the CRC to
   the host's bare frame, exactly what `0x0800E3E0` does for its 2-byte READ
   frame (rounds 3/4 wrongly cleared this and the tag never answered);
3. `set_bits(reader, 0x13, 0x80)` — RxCRCEn on, so the receiver verifies and
   **strips** the tag's 2 CRC bytes (16 data bytes survive → op 5 = `0x80`);
4. `0x0800E974(reader, 10)` — the firmware's 10 ms transceive timeout;
5. `set_bits(reader, 0x2A, 0x80)` — TModeReg TAuto, as `0x0800EB8C` does;
6. `ComIrqReg = 0x7F`, `ComIEnReg = 0xF7`;
7. preserve `BitFramingReg & 7`, `CommandReg = 0`, `CommandReg = cmd`,
   `BitFramingReg |= 0x80`, wait `ComIrq & 0x31`, then `BitFramingReg = 0`, read
   `ErrorReg`.

The `op 3` wait is bounded to the firmware's own **0xBB8 (3000) iterations**
and then always reads `ErrorReg` and clears StartSend, so a dead field returns
a status instead of hanging well before the host timeout.
`a1` is informational (whole TX bytes) and is not consumed by the stub.

## 4. Op 6 — reader/antenna selection, transmitter bring-up and SELECT

op 6 performs the sequence itself, mirroring the firmware's own recognition
bring-up (`0x0800F030` + `0x0800E314`).  Since round 5 the sequence is a shared
subroutine (`full_bringup`), also run by the op-1 TxModeReg trigger (§3), so
both paths bring the reader up identically:

```
0x0800F030()            all-reader pin/table setup + chip reset pulses
                        (= 0x0800EF58 + 0x0800EECC(0..3) + 0x0800EE60(0..3))
0x0800E228(4, 0)        all channel-select lines HIGH, as 0x08015A52 does
0x0800E228(reader, 1)   this channel's line LOW (selected), as 0x08015A5C does
0x0800E948(reader)      soft reset (CommandReg = 0x0F; ControlReg |= 0x10)
   osDelay(2)
0x0800E8E0(reader, 0x14, 0x03)
                        <<< TxControlReg |= 3 (read-or-write, UNCONDITIONAL)
0x0800E9EC(reader, 0)   component config (0x0C|=0x10, 0x15|=0x40, 0x12/0x13=0,
                        0x18=0x55, 0x26=0x78, 0x27=0xF8, 0x28=0x3F)
   osDelay(50)          let the field come up
0x0800E228(4, 0); 0x0800E228(reader, 1)   re-assert the selection
0x0800E314(reader, 0)   REQA (0x26, 7 bits) + anticollision CL1/CL2/CL3
                        + SELECT per cascade level, hardware CRC
   on non-zero result:
0x0800E314(reader, 1)   retry through the firmware's full mode-1 bring-up
                        (reset + antenna + config + the same SELECT)
```

Why the explicit `0x14` write (round 4) and why it is an OR (round 6): the
firmware's `0x0800EAEC(reader, 3)` reads `TxControlReg` and **returns early
without writing when bits 0..1 already read as set** (`0x0800EB0E..0x0800EB20`).
If that readback is stale, the antenna is never enabled and REQA times out.
The stub therefore writes the bits unconditionally — but with the firmware's
own read-or-write semantics (`0x14 |= 3`, bit `set_bits`): bits 0..1
(Tx1RFEn|Tx2RFEn) are always written and any other bit the chip had is
preserved.  Round 5 wrote `0x14 = 3`, which cleared every other bit; the
firmware never does that.

**Fallback (round 6).**  If the select-only entry returns non-zero, op 6 calls
`0x0800E314(reader, 1)` once — the firmware's full mode-1 path, i.e. exactly
what the stock recognition calls on a channel it can read.  The value returned
to the host is the *last* attempt's code.

**Reader selection is part of this op** (round 6): the `(4, 0)` + `(reader, 1)`
pair is the firmware's own per-channel selection, re-derived from
`0x0800E228`'s jump table and the dispatcher call sites; the line/mask map and
the SPI descriptor split are documented in §6.

Return: `0` = a card is in ACTIVE state; `2` = collision / more than one tag;
`0xFF` = no card or a failed exchange.  The old hand-rolled
REQA/anticollision/SELECT and software CRC_A are gone: the firmware routine
handles the 7-byte UID (e.g. the unit's `53427001B50001`) via cascade levels 1
and 2.

## 5. Op 5 — received bits

```
bytes = FIFOLevelReg(0x0A) & 0x7F
last  = ControlReg(0x0C) & 0x07
count = bytes*8            if last == 0
count = (bytes-1)*8 + last otherwise
```

With `op 3` enabling RxCRCEn, a READ reply leaves exactly the 16 data bytes in
the FIFO, so `op 5` reports `0x80` (128) — the value the host expects.

## 6. Reader/chip selection, enable and ownership

**How a reader chip is addressed.**  The `reader` field (index bits 24..25) is
the first argument of `read_reg 0x0800E734` / `write_reg 0x0800E7C0`.  Both
index two per-reader descriptor tables in RAM (`0x20006240` / `0x200062A0`,
stride `0x18`, entries 0..3) that hold the per-channel **bit-banged SPI pin
masks and GPIO bases** (`spi_send 0x0800E698`, `spi_recv 0x0800E600`).  There
is no separate chip-select call: passing `reader` is the selection.

**Per-channel selection: the firmware path (round 6 derivation).**  The
recognition dispatcher is `0x08015C94` (state byte `0x200071EC`); for a slot
it runs the state-11 handler `0x080159E4`, which walks the slot list and, for
each entry, calls at `0x08015A52`/`0x08015A5C`:

```
0x0800E228(4, 0)        all four lines HIGH
0x0800E228(slot-1, 1)   the slot's line LOW
```

and then requests the read event (`0x08011D88(5)` / `(6)` for the two tag
protocols, slots 1..4 / 5..8).  The reader action itself is state 6/14
(`0x08015AC0`), which after `0x0800F030` + a reset pulse calls the same
`0x0800E314(reader, 1)` for the channel (`0x08015B56`).  So for one slot the
firmware selects `line = slot index` and uses `reader = slot index`.

`0x0800E228(idx, val)` is a jump table over `idx` 0..4 (constants at
`0x0800E30C` = GPIOB, `0x0800E310` = GPIOA; `0x0800E1C4`, called from
`0x08012B50`, configures the pins as outputs and drives them LOW):

```
case idx 0..3:  val == 1 -> BRR  (GPIOx_BRR, drive the line LOW)
                val == 0 -> BSRR (GPIOx_BSRR, drive the line HIGH)
                line 0 = GPIOB bit 10 (PB10)
                line 1 = GPIOB bit 11 (PB11)
                line 2 = GPIOA bit 14 (PA14)
                line 3 = GPIOA bit 13 (PA13)
case idx 4:     val is a 4-bit mask, bit i -> line i, 1 = LOW
```

`(4, 0)` is therefore exactly equivalent to setting all four lines HIGH, and
`(slot, 1)` to setting one line LOW; `(4, 1 << slot)` is the same selection in
one call.  The firmware drives `(4, 0)` first so that the other channels are
deselected while this one is active.

**SPI side.**  `reader` is the index into the SPI descriptor tables
(`0x20006240` masks / `0x200062A0` GPIO bases, stride `0x18`, entries 0..3,
built by `0x0800EF58`).  Each entry holds six pins: CS (`+8`), CLK (`+12`),
MOSI (`+16`), MISO (`+20`) and the chip reset/power pins (`+0`, `+4`) pulsed
by `0x0800EE60` inside `0x0800F030`.  Entries `{0,2}` and `{1,3}` are
byte-identical: CS = PB12 for readers 0/2, CS = PC6 for readers 1/3, with the
chip reset pulses PC9 (pair 0/2) and PA8 (pair 1/3).  A channel is addressed
purely by passing `reader` — there is no separate chip-select call.

**Hardware cross-check.**  Gen 1 carries two `NFC_V0.4` reader boards, each
with one FM17580 and two antenna channels (see `ACEResearch/HARDWARE.md` and
the board photo); the packed `reader` index enumerates the four channels in
the physical order spool 1, 3, 2, 4 (authoritative map at the end of this
file).  The SPI descriptor split `{0,2}`/`{1,3}` matches the board pairing
(chip A serves channels 0/2, chip B channels 1/3).

**Bring-up and the transmitter.**  A channel answers SPI (`VersionReg`) without
bring-up, but does not transmit until it is soft-reset, **TxControlReg (0x14)
bits 0..1 are set**, and the component config is applied.  The firmware does the
antenna enable in `0x0800EAEC(reader, 3)`, but that routine reads 0x14 and
returns early when bits 0..1 already read as set (`0x0800EB0E..0x0800EB20`), so
it can skip the actual write.  op 6 therefore ORs the bits unconditionally with
the firmware's own semantics (`0x14 |= 3`; §4).

**Reader ownership.**  The recognition state machine is dispatched from the
main task (`0x08013790` loop -> `0x08015C94`) every ~100 ms, keyed on the state
byte `0x200071EC` (`state_get 0x08011E98` / `state_set 0x08011EA4`).  State 0 is
idle: the dispatcher subtracts 1, compares `> 13` and returns.  States 5 and 13
run the reader (`0x08015AC0`).  Nothing in the firmware takes a lock, because
the reader lives in a single task; a second task (our JSON handler) must
therefore keep it out of those states:

* **every op** captures `0x200071EC` at entry, drives it to 0, and restores
  the captured value on **every reply path** (round 6) — an unknown op or the
  marker no longer leaves the machine paused;
* **op 7 (acquire)** is the explicit hold: it captures the state, writes 0,
  `osDelay(200 ms)` so an in-flight action finishes, writes 0 again, returns
  the captured value, and its reply restores 0 (the hold);
* **op 8 (release)** writes `a2` (the value op 7 returned) back;
* run multi-command sequences (SELECT+READ) **inside an op-7 hold**: each op
  pauses the machine at entry, but an action that was already in flight when
  the first op started still needs the 200 ms drain.  With the round-6 restore
  the machine resumes between commands (the dispatcher period is ~100 ms, so
  back-to-back tunnel commands stay inside one slot); a leaked hold is no
  longer possible.

**Why round 2 read a constant `0x19` from `op 4`.**  A real FIFO read advances
the FIFO pointer, so 18 consecutive reads cannot return 18 identical bytes; a
constant is a read that is *not* a FIFO data read.  The consistent explanation
with the rest of the round-2 evidence (SELECT `0xFF`, `op 5 = 1`, `op 2`
accepted) is that the recognition task, running concurrently, kept
soft-resetting/flushing the chip: the FIFO was empty at every read, so `0x19`
is the empty/locked-FIFO signature of this chip, not card data.  Round 3 makes
the check decisive: with the reader held (op 7), `op 1 0x0A = 0x80` (flush),
`op 2 = 0x5A`, `op 0 0x0A` (level) and `op 4` must return `0x5A` and level 1.
If it still returns a constant, the failure is in the FIFO register path and
the recorded level (`0x0A`) tells which stage broke.

## 7. Controller commands and expected outputs

The probe tool computes the signed form automatically (`probe_tunnel.as_signed32`,
R10).  A `code` is only present when the stub ran (tunnel index), which also
proves the early hook fired.

### 7.0 Image / reachability

```text
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0
    # expect "firmware":"CV1.3.871"  (proves the new image is live)
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=1000000
    # expect {"id":<N>,"result":{"code":90},"msg":"ok"}  (marker, unchanged)
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2
    # expect the stock success reply (no result.code), unchanged behaviour
```

### 7.1 Take the reader (op 7), then the FIFO self-test

```bash
python3 probe/probe_tunnel.py acquire  --dev <port>
    # op7 -> saved recognition state (pass it to release)
python3 probe/probe_tunnel.py fifo-test --dev <port> --byte 0x5A
    # expect: level before=0 after flush=0 after write=1 read_back=90 raw=... -> OK
```

Raw equivalent of the self-test (ops 0/1/2/4; all inside the op-7 hold):

```text
ACE_EXT_RAW ... INDEX=-2147481088   # 0x80000A00 op0 read 0x0A -> 0 (empty)
ACE_EXT_RAW ... INDEX=-2147415424   # 0x80010A80 op1 write 0x0A = 0x80 (flush)
ACE_EXT_RAW ... INDEX=-2147352486   # 0x8002005A op2 write 0x5A
ACE_EXT_RAW ... INDEX=-2147481088   # 0x80000A00 op0 read 0x0A -> 1
ACE_EXT_RAW ... INDEX=-2147221504   # 0x80040000 op4 -> 0x5A
```

(The tool computes these; use `probe_tunnel.py fifo-test`.)

### 7.2 Register write-then-read-back (ops 1 then 0)

```text
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=-2147414784
    # 0x80010D00  op1 write 0x0D = 0x00 -> expect result.code = 0
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=-2147480320
    # 0x80000D00  op0 read  0x0D      -> expect result.code = 0
```

### 7.3 VersionReg on all four readers (op 0)

The packed `reader` field is the channel index (0..3), so the same op with
`reader` in bits 24..25 walks every channel.  Run each read inside an op-7 hold
(or at least on an idle unit):

```text
INDEX=-2147469568   # 0x80003700 op0 reader0 reg 0x37 -> 161 (0xA1)
INDEX=-2130692352   # 0x81003700 op0 reader1 reg 0x37 -> 161 (0xA1)
INDEX=-2113915136   # 0x82003700 op0 reader2 reg 0x37 -> 161 (0xA1)
INDEX=-2097137920   # 0x83003700 op0 reader3 reg 0x37 -> 161 (0xA1)
```

Every channel should answer `0xA1`.  A channel that reads `0x00`/`0xEF`/`0xFF`
is not answering on its SPI pair (PB12 for 0/2, PC6 for 1/3) — reseat the
board before blaming the tunnel.  (Round 3 observed `0xA1` on readers 0 and 1;
readers 2/3 were never probed, this is the first sweep that covers all four.)

### 7.4 SELECT (op 6) on every reader — the acceptance probe

**Run the whole sweep inside one op-7 hold.**  The stock recognition machine
runs every ~100 ms; a SELECT issued while it is mid-action races it (soft
reset + FIFO flush between the stub's setup and its transceive) and returns
255.  That is a probe-sequencing artifact, not a tunnel failure — and it is
exactly what a `INDEX=0..3` stock kick right before the probes causes.

```bash
python3 probe/probe_ntag.py select-scan --dev <port>
    # acquire -> saved state <N>
    # reader 0: SELECT code(s) [0]   -> card present
    # reader 1: SELECT code(s) [0]   -> card present
    # reader 2: SELECT code(s) [255] -> no card
    # reader 3: SELECT code(s) [255] -> no card
    # release -> restored state <N>
```

(Example output.  The report's `sku` array does not state which physical coil a
tag is on; which readers answer depends on where the tags are — the sweep is
the live per-channel check.)

The `--readers 0,1,2,3` and `--repeat N` options narrow or repeat the sweep.
For a failed reader the tool also prints the stage registers (see §7.6):

```text
reader 1: SELECT code(s) [255] -> no card
          stages: ComIrqReg=0x01, ErrorReg=0x00, FIFOLevelReg=0x00
```

Raw equivalent (signed `INDEX`, one hold around all four):

```text
# acquire once:        INDEX=-2147024896   (0x80070000) -> saved state
# reader 0 (spool 1):  INDEX=-2147090432   (0x80060000) -> 0 with a tag
# reader 1 (spool 3):  INDEX=-2130313216   (0x81060000) -> 0 with a tag
# reader 2 (spool 2):  INDEX=-2113536000   (0x82060000) -> 0 with a tag
# reader 3 (spool 4):  INDEX=-2096758784   (0x83060000) -> 0 with a tag
# release:             INDEX=-2146959352   (0x80080000 | saved)
# empty field -> non-zero (2 = collision, 255 = no card), device stays
# responsive (no hang)
```

A single reader can be probed with `probe_ntag.py select --dev <port>
--reader N`; on a repeatedly-probed unit wrap it in
`probe_tunnel.py acquire` / `release` manually.

Expected per reader, with a tag parked at that channel: `result.code = 0`.
With no tag: `2` or `255`, and the next command still answers (the code is
bounded, nothing hangs).  The physical map (which spool is which channel) is
at the end of this file.

### 7.5 NTAG READ(0x30, page 0) — 16 bytes

Keep the op-7 hold across these two commands, then release:

```bash
python3 probe/probe_ntag.py select    --dev <port> --reader 0
python3 probe/probe_ntag.py read-page --dev <port> --page 0 --reader 0
python3 probe/probe_tunnel.py release --dev <port> --state <saved>
    # page=0 frame=3000 status=0 bits=128
    # data[16] = <32 hex chars>
```

or in one process, with the op-7 hold spanning SELECT and READ automatically:

```bash
python3 probe/probe_ntag.py read-full --dev <port> --page 0 --reader 0
    # acquire -> saved state <N>; SELECT -> code=0
    # page=0 frame=3000 status=0 bits=128
    # data[16] = <32 hex chars>
```

`read_page` runs op 6 SELECT, op 1 flush + clear + BitFraming, op 2 pushes the
bare `30 00` frame, op 3 TRANSCEIVE, op 5 RX-bits, op 4 pops 16 bytes — the
same frame shape the multiACE host uses (`_rc_setup_crc` + `_rc_read_page`).
The RC522 appends the CRC (op 3 sets TxCRCEn) and strips the reply CRC
(RxCRCEn), so a full reply is exactly 16 bytes.

Expected: `status=0`; `bits=128` (`0x80`, full 16-byte frame); the 16 bytes are
pages 0..3 of the tag.

**Cross-check with the logged UID `53427001B50001`** (7-byte UID, cascade 1+2):

```
page 0 : 53 42 70 01                       UID0..3
page 1 : E8 B5 00 01                       BCC0, UID4, UID5, UID6
page 2 : 3C ?? ?? ??                       BCC1, internal, lock bytes
page 3 : E1 10 ?? 00                       capability container
BCC0 = 0x88 ^ 53 ^ 42 ^ 70 ^ 01 = 0xE8
BCC1 = 0x88 ^ B5 ^ 00 ^ 01      = 0x3C
```

So the first 8 bytes must be `53 42 70 01 E8 B5 00 01`; page 2 bytes 1..3 and
page 3 depend on the tag model.

### 7.6 If it still fails — the exact per-step register reads

**First: which stage failed?**  op 6 runs the firmware's REQA + anticollision +
SELECT; when it returns non-zero, read these three registers (inside the hold)
to localise the stage:

| reading | stage | interpretation |
|---|---|---|
| `0x04 ComIrqReg = 0x01` (TimerIRq only) and `0x0A = 0` | **REQA** | no ATQA: no tag in that channel's field, field off, or the wrong channel selected |
| `0x04 = 0x20/0x30` (RxIRq) but `0x06 ErrorReg != 0` | **anticollision / SELECT** | a tag answered but the frame was corrupt: `0x08` CRCErr (TxCRCEn/RxCRCEn or antenna), `0x10` CollErr (two tags) |
| `0x04 = 0x20/0x30`, `0x06 = 0`, `0x0A != 0` | **SELECT** | data arrived but the firmware's cascade state machine did not finish; read the FIFO to see how far it got |
| `0x37 VersionReg` not `0x91/0x92/0xA1` | **SPI** | the channel does not answer on its pair (PB12 for 0/2, PC6 for 1/3) |

`probe_ntag.py select-scan` prints these three registers automatically for
every reader whose SELECT returned non-zero.  If ALL four readers fail, check
the probe sequence first (§7.4): the stock recognition task must not have been
kicked (`INDEX=0..3`) right before, and the sweep must run inside one op-7
hold.  op 6 now retries through the firmware's full mode-1 bring-up on a failed
first attempt, so a transient field/setup problem is covered by the stub.

Run everything inside the op-7 hold, `reader 0` unless stated, and read
registers with **op 0** (`a1` = register).  The reads follow the host's
own sequence (op 6 → op 1 TxMode/RxMode/BitFraming → op 2 ×2 → op 3 → op 5 →
op 4), so a wrong intermediate value is caught at the step that wrote it.

Signed `INDEX` for each read:

| op | call | INDEX |
|---|---|---|
| 0 | read 0x04 ComIrqReg | `-2147482624` |
| 0 | read 0x06 ErrorReg | `-2147482112` |
| 0 | read 0x0A FIFOLevelReg | `-2147481088` |
| 0 | read 0x0C ControlReg | `-2147480576` |
| 0 | read 0x0D BitFramingReg | `-2147480320` |
| 0 | read 0x12 TxModeReg | `-2147479040` |
| 0 | read 0x13 RxModeReg | `-2147478784` |
| 0 | read 0x14 TxControlReg | `-2147478528` |
| 0 | read 0x37 VersionReg | `-2147469568` |
| 1 | write 0x12 = 0x80 | `-2147413376` |
| 1 | write 0x13 = 0x80 | `-2147413120` |
| 1 | write 0x0D = 0x00 | `-2147414784` |
| 2 | FIFO write 0x30 | `-2147352528` |
| 2 | FIFO write page | `-2147352320` |
| 3 | TRANSCEIVE a1=2 a2=0x0C | `-2147286516` |
| 5 | RX bits | `-2147155968` |
| 4 | FIFO read | `-2147221504` |
| 6 | SELECT | `-2147090432` |

**Step A — after `op 6` returns 0 (card ACTIVE):**

| reg | expect | meaning of anything else |
|---|---|---|
| 0x37 VersionReg | `0xA1` | not 0x91/0x92/0xA1 → wrong chip/CS on the shared SPI pins |
| 0x14 TxControlReg | bits 0..1 set (`0x03`, or `0x83` if the chip had bit 7) | bits 0..1 clear → the antenna write did not stick; the round-6 write is an OR and always lands |
| 0x04 ComIrqReg | RxIRq `0x20` (often with IdleIRq `0x10`) | only TimerIRq `0x01` → the last SELECT cascade timed out; stale `0x04` clears on the next op 3 |
| 0x06 ErrorReg | `0x00` | `0x08` CRCErr / `0x10` CollErr → the SELECT frame itself is bad |
| 0x0A FIFOLevelReg | `0x00` | non-zero → stale bytes; they will prepend the host's frame (the host does not flush) |
| 0x0C ControlReg | `0x00` | non-zero last-bits together with 0x0D |
| 0x0D BitFramingReg | `0x00` (7 right after a REQA helper) | non-zero → the host's op 1 `0x0D = 0` is the fix; a non-zero value at op 3 means the frame was sent with partial bits |

**Step B — after `op 1 0x12 = 0x80` (the bring-up trigger, §3):**

* `0x14` = `0x03`, `0x37` = `0xA1`, `0x0A` = `0x00` — the trigger ran
  `full_bringup` (reset + config) and the host's write landed afterwards.
* `0x12` read-back = `0x80` — if it reads `0x00`, the write landed *before*
  the bring-up or not at all: the CRC is off and op 3's `set_bits(0x12,0x80)`
  must be checked next.
* `0x13` read-back = `0x80`, `0x0D` = `0x00`.

**Step C — after the two `op 2` writes:**

* `0x0A` = `0x02` — exactly the bare frame (`30 <page>`); `>2` = stale bytes
  joined it; `0` = a FIFO write was lost.
* `0x0D` = `0x00`; `0x14` = `0x03`; `0x37` = `0xA1` (nothing reset the chip
  between op 2 and op 3).

**Step D — right after `op 3`:**

| reg | expect | meaning of anything else |
|---|---|---|
| 0x04 ComIrqReg | RxIRq\|IdleIRq `0x30` (the op-3 wait mask) | only TimerIRq `0x01` → **no reply**; only IdleIRq `0x10` → command aborted; nothing → `0xBB8` iterations ran out before the 10 ms timer (raise `WAIT_ITERS`) |
| 0x06 ErrorReg | `0x00` | `0x08` CRCErr → RF seen but the frame/CRC is wrong (check `0x12` bit7, `0x13` bit7, hardware vs software CRC); `0x10` CollErr → more than one tag |
| 0x0A FIFOLevelReg | `0x10` (16 data bytes; RxCRCEn stripped the 2 CRC) | `0` → nothing arrived (frame never reached a tag / field off — re-check steps A..C); `2`/`4` → short or NAK reply; `0x12` → RxCRCEn is *not* stripping (check `0x13` bit7) |
| 0x0C ControlReg | `0x00` | non-zero → partial last byte; op 5 folds it in |
| 0x0D BitFramingReg | `0x00` | non-zero → StartSend still set, transceive aborted mid-frame |

**Step E — after the 16 `op 4` reads:**

* `0x0A` = `0x00` — the read pointer advanced through exactly the received
  bytes; a non-zero value means the host read a different count than op 5
  reported.

If D shows “TimerIRq only” while A..C are clean, the frame was transmitted but
no tag answered: confirm the park (the stock `filament_recognition INDEX=0`
cycle), try `op 6` on readers 0..3 (`INDEX -2147090432, -2130313216,
-2113536000, -2096758784`); any `0` identifies the tag's antenna.  If `0x12`
bit7 still reads 0 after `op 1 0x12 = 0x80`, the op-1 trigger is not firing
(re-check the installed image: `get_info` must say `CV1.3.871`).

### 7.7 Regressions

```text
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0            # firmware CV1.3.871
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2   # stock reply, no code
ACE_EXT_RAW METHOD=get_filament_info INDEX=0 FORCE=1 ACE=0      # unchanged tag record
```

### 7.8 Rollback

Reflash the verified tunnel reference `ACE_V1.3.863_tunnel_ops.bin`
(`CV1.3.871`, md5 `219df3df77f7c7e1e15a79d580a2379e`, 114632 B, crc16
`0x1AC9`, 804 B stub, 56 B headroom), or `ACE_V1.3.863_cfw_uid.bin` (md5
`6148cfc52431fc235536c6d64b5334ef`, 113828 B, crc16 `0xAE92`) to return to the
UID-only image.  Ruling R18: `CV1.3.871` stays the accepted reference until a
later image is re-accepted.

---

## 8. Size ceiling (firmware property — assert, never eyeball)

The app starts at `0x08008000` and the IAP staging base is `0x08024000`, so the
applied image must be **≤ 0x1C000 = 114688 bytes**.  The base is 113720 B,
leaving a 968 B tail budget for the frozen 108 B parser stub plus the extra
stub.  **An oversized image is accepted by `iap_upgrade` (it still answers
`code: 0, "success"`) but never commits** — the unit keeps running the previous
image.  The builder refuses any output over `MAX_IMAGE_BYTES` with a
`ValueError` naming the size and the budget, covered by an offline test.

This stub is 804 B → image 114632 B, **56 B under the ceiling**.

## 9. Risks / open items

* **Round 6 is device-verified (§0b).**  The selection re-derivation matches
  the firmware's own calls (line = slot = reader, §6) and the antenna write
  preserves bits (`0x14 |= 3`); the earlier failures were the antenna path.
  If a reader ever fails while its stock read succeeds, run
  `probe_ntag.py select-scan` inside one op-7 hold (§7.4) and send the stage
  registers (§7.6) — they say whether the failure is REQA,
  anticollision/SELECT or SPI.  An RX-path rework is *not* part of this
  version; the round-7 experiment lives in git history (`3d598a3`).
* **The probe sequence matters**: never kick stock recognition (`INDEX=0..3`)
  right before a tunnel SELECT — the kicked action races the stub for ~100 ms.
  Use one acquire/scan/release sweep.
* **`op 1 0x12` runs the full bring-up** (soft reset + config + SELECT) before
  the host's write lands, so the reader is up before the host loads its frame.
  A host that writes TxModeReg in the *middle* of a frame (after op 2) would
  lose that frame.  The reference host (`ace_rc522.py::_rc_setup_crc`) always
  writes it first; no other write path was found in the Gen-1 debug host.
  (This trigger also takes the round-6 antenna/retry changes.)
* **Ownership is restorable, not a lock.**  op 7 drains 200 ms and every op
  pauses the dispatcher at entry, but an action already in flight when the
  first op starts can overlap it; the sweep/release design covers that.  The
  round-6 restore means no reply path can leave the machine paused.
* **`0x0800E228` polarity** is derived from the jump table as active-low select
  (1 = line LOW), matching the firmware's call sites (`0x08015A52`/
  `0x08015A5C`, `0x0801382A` main loop).  If a channel stops answering after a
  selection change, that call is the first suspect.  Round 6 re-asserts the
  selection on the RF paths (op 3 and the bring-up), not on register/FIFO ops.
* **`op 4` returning a constant after a command is the empty-FIFO signature**
  (the self-test proves the write/level/read path); read `0x0A` to confirm the
  FIFO is empty rather than mis-read.
* **The host frame is CRC-less by design**: op 2 pushes only `30 <page>` and
  op 3 sets TxCRCEn so the RC522 appends CRC_A.  A host that wants to supply
  its own software CRC must clear TxCRCEn *after* op 1 (not done by the
  reference host) or the frame gets a double CRC.
* **`op 3`'s `a1` is unused** (the host pre-loads the FIFO via op2).
* `op 6` and the op-1 trigger perform a soft reset each call (firmware
  bring-up), so SELECT and a following READ must be issued back to back inside
  the op-7 hold.
* The marker range (`bit31 clear && index >= 0x10000`) is diagnostic only;
  since round 6 it also pauses and restores the recognition state.
* `op 8` restores the state from `a2`; if a probe session is abandoned without
  a release, op 7's hold keeps the stock NFC task paused until the next reboot
  / enable — a plain tunnel op no longer does (it restores what it captured).

## ANTENNA MAP (Gen 1, подтверждено владельцем юнита — ЗАПОМНИТЬ)

| `reader` в packed-индексе | физическая антенна | катушка (1-based слот) |
|---|---|---|
| 0 | антенна 0 | катушка 1 |
| 1 | антенна 1 | катушка 3 |
| 2 | антенна 2 | катушка 2 |
| 3 | антенна 3 | катушка 4 |

Соответствие линий/масок (firmware, §6):

| `reader` | канал 0x0800E228 | линия GPIO | SPI (CS) | плата |
|---|---|---|---|---|
| 0 | `(0, 1)` — низкий | PB10 | PB12 | чип A |
| 1 | `(1, 1)` — низкий | PB11 | PC6 | чип B |
| 2 | `(2, 1)` — низкий | PA14 | PB12 | чип A |
| 3 | `(3, 1)` — низкий | PA13 | PC6 | чип B |

`(4, 0)` перед выбором поднимает все четыре линии, `(4, 1 << reader)` —
эквивалент выбора одним вызовом. `0x0800E228(idx, 1)` = BRR (низкий),
`0x0800E228(idx, 0)` = BSRR (высокий); в `0x0800E228(4, mask)` бит i = 1
означает линию i в низкий.

ВАЖНО: на Gen 1 НЕТ модели «reader = slot >= 2» из ACE2-драйвера — считыватель/антенна
у каждого слота свои (4 канала). `reader` = индекс слота (0-based). Legacy-предположение
«пары 1&3 / 2&4» — НЕВЕРНО.  Аппаратно это две платы `NFC_V0.4` по одному FM17580
на каждой, у каждой по две антенны (см. `ACEResearch/HARDWARE.md`); пары SPI-дескрипторов
`{0,2}` (CS PB12) и `{1,3}` (CS PC6) — это и есть две платы, а линии 0..3 выбирают канал.
