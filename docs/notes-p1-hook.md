<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 ACE-UID contributors -->

# Task 2 / probe P1 — hook site in `filament_recognition` + probe image

> **Superseded by P2:** live testing showed the packed `INDEX` never reaches the
> handler with bit 31 set (the firmware parses `params.index` with signed
> `strtol` and clamps ≥ 2³¹ to `0x7FFFFFFF`). The stub was then extended to
> deliver the register value in `result.code`; see `docs/notes-p2-reply.md` and
> the P2 image `ACE_V1.3.863_probe_p2.bin`. The P1 image recorded in this file is
> kept for history and its hook site is unchanged.

This is the offline deliverable of probe P1: the chosen hook site, the interface
Task 4/5 will build on, the exact probe image, and the commands the controller
runs on the printer. **No device was touched from this session.**

Base image: `ACE_V1.3.863_20260716.bin`, md5 `9f7b9a678a96caf98d6a08842d3ff971`,
113720 B (VA = file offset + `0x08008000`).
Working image: `ACE_V1.3.863_cfw_uid.bin`, md5 `6148cfc52431fc235536c6d64b5334ef`,
113828 B (frozen parser hook + 108 B stub).

---

## 1. The command and its handler

The ACE-protocol command table is a sequence of `{handler_thumb_ptr, name_ptr}`
pairs. The pair for our carrier:

```
VA 0x08014674  handler = 0x080144D5 (Thumb)  ->  function VA 0x080144D4
VA 0x08014678  name    = 0x08021508          ->  "filament_recognition"
```

(neighbours: `enable_rfid` 0x0801467C, `disable_rfid` 0x08014684,
`get_filament_info` 0x0801468C, `set_filament_info` 0x08014694.)

### Handler calling convention (established from the dispatcher's call sites)

At handler entry:

| reg | meaning |
|---|---|
| `r0` | JSON-RPC request id (goes into the response `%d`) |
| `r1` | JSON params object, passed to the parser helper |
| `r2` | parser key argument |
| `r3` | **response/output buffer** pointer |

The handler parses its single parameter and remembers the id and buffer:

```
080144d4  push  {r4, r5, r6, r7, lr}
080144d6  mov   r4, r1            ; r4 = params object (parser arg)
080144d8  sub   sp, #20
080144da  mov   r6, r0            ; r6 = request id
080144dc  mov   r0, r4
080144de  mov   r4, r3            ; r4 = response buffer
080144e0  add   r3, sp, #12       ; &local_index_struct
080144e2  mov   r1, r2
080144e6  ldr   r2, [pc, #68]     ; r2 = 0x08021068 "params.index"
080144e8  str   r3, [sp, #0]      ; 5th arg = &local (12-byte) struct
080144ea  movs  r3, #12
080144ec  str   r5, [sp, #12]     ; zero the first 4 bytes
080144ee  bl    0x0801863c        ; parse params.index -> local at [sp,#12]
080144f2  bl    0x08011d74        ; gate on the device state byte
080144f6  cbz   r0, 0x08014502    ; not busy -> FORBIDDEN response
080144f8  ldr   r5, [pc, #52]     ; r5 = 0x200072d4 (device ptr)
080144fa  ldr   r3, [r5, #0]
080144fc  ldrb  r3, [r3, #0]      ; device state byte
080144fe  cmp   r3, #3
08014500  bne.n 0x08014512        ; idle -> success path
08014502  ldr   r3, [pc, #48]     ; 0x080210FC "FORBIDDEN"
08014504  ldr   r1, [pc, #48]     ; 0x08021130 response format
08014506  mov   r2, r6            ; id
08014508  mov   r0, r4            ; response buffer
0801450a  bl    0x0801dc74        ; <-- HOOK HERE
0801450e  add   sp, #20
08014510  pop   {r4, r5, r6, r7, pc}

08014512  ldrb.w r7, [sp, #12]    ; index & 0xFF  (success path)
08014516  movs  r0, #7
08014518  bl    0x08011d88        ; log
0801451c  mov   r0, r7
0801451e  bl    0x08011ed4        ; set active slot (valid 0..3 / 0xFF)
08014524  ldr   r3, [pc, #20]     ; 0x08020C7C "success"
08014526  movs  r1, #2
08014528  strb  r1, [r2, #0]
0801452a  b.n   0x08014504
```

`0x0801863C` stores the parsed integer as a **32-bit little-endian word** into
the handler's local struct (`str r0,[r1,#0]`), i.e. at handler `[sp,#12..15]`.
`0x0801DC74` is the shared response formatter
`respond(buffer, fmt, vararg1, vararg2, ...)` (a printf-style JSON writer).

## 2. Chosen hook site

```
HOOK VA          0x0801450A        (inside the filament_recognition handler)
ORIGINAL BYTES   09 F0 B3 FB       (file order)
                 f009 fbb3         (halfword/datasheet convention)
DISASSEMBLY      bl 0x0801DC74     (the handler's response call)
REPLACED WITH    0F F0 CB FB       (file order)  = bl 0x08023CA4
```

Why this site:

* it is a **4-byte `bl`**, so it is replaced by another 4-byte `bl` — no
  instruction is split and every register contract is preserved;
* it is reached on **both** handler exits (idle/success and FORBIDDEN) after
  `params.index` has been parsed, so the packed probe works regardless of the
  device state byte;
* it hands the stub everything it needs: the parsed index is live in the
  handler frame, and `r0`/`r2` already hold the response buffer and request id.

### Side-effect-free argument (regression safety)

* Ordinary slot indices are small (`< 0x20000`); bit 31 is clear. On that path
  the stub tail-calls the original `0x0801DC74` with `r0..r3` untouched, so the
  response is **byte-for-byte identical** to the unpatched handler.
* The stub writes only `r12` (caller-saved scratch) and `r0..r3` (argument
  registers, dead after the call site). The packed/sentinel paths also use `r4`
  and `r5`, but that is safe: the handler epilogue (`pop {r4,r5,r6,r7,pc}`)
  restores them from the handler's own frame, so the caller still sees the
  original values. The stub also restores `lr`.
* The handler's stack frame is only *read* (at `[sp,#12]` in the stub); the
  parser result is not modified.
* The packed path is unreachable for any host that does not set bit 31.
* The appended stub lives in the free image tail and overlaps no existing data;
  the frozen parser hook (`0x08016B9A`) and its 108-byte stub are untouched.

### Interface for Task 4/5 — how the handler receives the decoded `index`

* JSON key: `"params.index"` (string at VA `0x08021068`).
* Parse result: 32-bit little-endian word in the handler's local struct at
  **handler `[sp,#12]`** (zeroed at `0x080144EC`, written by `0x0801863C`).
* At the hook the handler has already pushed 20 B and `sub sp,#20`.  On
  Cortex-M `bl` does **not** push to the stack, so the stub is entered with the
  handler's SP and the word is at **stub `[sp,#12]`** (`[sp,#16]` was the
  P1/P2 bug; see `docs/notes-p2-reply.md` §9).
* The success path reads the low byte with `ldrb.w r7,[sp,#12]`
  (`0x08014512`) and passes it to `0x08011ED4` (active-slot setter, accepts
  `0..3` / `0xFF`); `r7` is dead after `0x0801451E`.
* Response value channel: the formatter `0x0801DC74` with a printf-style format.
  The handler's own format is `0x08021130`
  = `{"id":%d,"result":{},"code":0,"msg":"%s"}`. The probe passes a custom
  format so the byte lands in **`result.code`**, exactly where the multiACE host
  reads it (`resp['result']['code'] & 0xFF`).

### Known benign side effect of a packed probe

Because the handler does not mask bit 31 before the success path, a packed
probe that reaches `0x08014512` still runs the normal success-path tail
(`ldrb.w r7,[sp,#12]` → low byte 0x00 → `0x08011ED4` sets the active slot to 0,
then the device state byte is set to 2 at `0x08014528`, i.e. a slot-0
recognition cycle is queued). This is harmless for a debug probe: the response
value is produced by our stub and the extra work is an ordinary slot-0 read.
On the FORBIDDEN path none of this runs. Task 4/5 should mask bit 31 in the
decoded index (or hook earlier) before this becomes a production path.

## 3. Firmware primitive addresses — important correction

The brief quotes the primitives as `read_reg 0x08019D12`, `write_reg 0x08019E1C`,
identify `0x0800A6A0`. **Those are *stock*-image addresses** (the old stock
`ACE_V1.3.863_stock.bin` tunnel route, REPORT-EN.md Appendix B). In the CFW base
`ACE_V1.3.863_20260716.bin` all three land **mid-function** and must not be
called:

| VA | stock | CFW (`ACE_V1.3.863_20260716.bin`) |
|---|---|---|
| `0x08019D12` | `push {r4,r5,r6,lr}` — `read_reg(reader, reg)` | `uxtb r7,r7` inside a retry loop |
| `0x08019E1C` | `push {r4,r5,r6,lr}` — `write_reg(reader, reg, val)` | `bl 0x08011d88` (a log call) |
| `0x0800A6A0` | tag-identify body | `blx r3` inside a different function |

Correct CFW primitives (used by a new stub):

| primitive | CFW VA (Thumb entry) | evidence |
|---|---|---|
| `read_reg(reader, reg) -> byte` | `0x0800E734` (`0x0800E735`) | the firmware itself calls it at `0x0800E8CC` with `reg=0x37` to probe VersionReg on each reader |
| `write_reg(reader, reg, value)` | `0x0800E7C0` (`0x0800E7C1`) | same pin table (stride 24) and RC522 address framing |
| response formatter | `0x0801DC74` (`0x0801DC75`) | called 47× as `respond(buf, fmt, ...)` |

Distinguishing proof for `read_reg`: its address byte is computed as
`(reg << 1) | 0x80` (`lsls r1,r7,#1; orn r1,r1,#127; and r1,#254`) — the RC522
read frame — and it returns the transferred byte.

## 4. Probe stub (`artefacts_stub_probe.s`)

Behaviour:

* `ldr.w r12,[sp,#12]` → `lsrs r12,r12,#31` → `beq normal`.
* bit 31 set → `read_reg(reader=0, reg=0x37)` (`0x0800E734`) and answer with
  `{"id":%d,"result":{"code":%d},"code":0,"msg":"success"}` via `0x0801DC74`,
  passing the request id and the register value.
* bit 31 clear → replay the displaced `bl 0x0801DC74` with the untouched
  `r0..r3` and return into the handler epilogue.

Assembled size: **116 B**, linked at VA `0x08023CA4` (right after the 108-byte
frozen parser stub). Tail budget: the probe adds 116 B to the frozen 108 B;
remaining headroom under the 968 B allowance is **852 B**.

## 5. Probe image

Built with:

```
python3 artefacts_build_rc522_tunnel.py ACE_V1.3.863_20260716.bin /tmp/probe-p1.bin \
    --hook-fixed --hook 0x0801450A --stub artefacts_stub_probe.s --version 1.3.863
```

Result (pinned by `tests/test_probe_hook.py`):

```
md5    a9b1e95faf8a3b0bc6b5aa02641c26a9
size   113944
crc16  0x30EC

parser hook 0x08016B9A  f8931036 -> 0df04df8   (frozen, unchanged; halfword convention)
probe  hook 0x0801450A  f009fbb3 -> f00ffbcb   (halfword convention)
                        09f0b3fb -> 0ff0cbfb   (file order) = bl 0x08023CA4
parser stub 108 B at VA 0x08023C38
probe  stub 116 B at VA 0x08023CA4
```

Changed byte ranges **vs the base** `ACE_V1.3.863_20260716.bin` (230 B total):

```
0x0801450A..0x0801450A   (1 B)   probe hook, byte 0
0x0801450C..0x0801450C   (1 B)   probe hook, byte 2  (bytes 1,3 unchanged)
0x08016B9A..0x08016B9D   (4 B)   frozen parser hook
0x08023C38..0x08023D17   (224 B) appended tail = 108 B parser stub + 116 B probe stub
```

Changed bytes **vs the working** `ACE_V1.3.863_cfw_uid.bin` (118 B total):

```
0x0801450A..0x0801450A   (1 B)
0x0801450C..0x0801450C   (1 B)
0x08023CA4..0x08023D17   (116 B) probe stub
```

`/tmp/probe-p1.bin` and the recorded image are byte-identical (verify with
`md5sum`).

## 6. Commands for the controller (live printer)

> No host edits are needed for P1: the value is delivered in `result.code`, the
> field the multiACE RC522 driver already reads. The frozen parser patch keeps
> ordinary third-party tags working (`rfid = 2`).

Replace `<printer>` with the printer host and keep the by-path device exactly as
below (it is the known-good ACE port path).

```bash
# 0. copy the image and the flasher to the printer, verify the image
rsync -av /tmp/probe-p1.bin <printer>:/tmp/probe-p1.bin
rsync -av ace_flash.py      <printer>:/tmp/ace_flash.py
ssh <printer> 'md5sum /tmp/probe-p1.bin'      # expect a9b1e95faf8a3b0bc6b5aa02641c26a9
```

```text
# 1. via the Klipper console / Mainsail macro console (NOT /etc/init.d/S60klipper!)
ACE_EXT_RAW METHOD=drying_stop FORCE=1 ACE=0      # stop any drying
ACE_EXT_FW_RELEASE ACE=0                          # release the serial port
```

```bash
# 2. flash (on the printer)
ssh <printer> 'python3 /tmp/ace_flash.py \
  /dev/serial/by-path/platform-fed00000.usb-usb-0:1.3.3:1.0 flash /tmp/probe-p1.bin'
# expect: image /tmp/probe-p1.bin: 113944 bytes, crc16=0x30EC
#         iap_upgrade: {'code': 0, 'msg': 'success'}
#         sent 113944 bytes in ~19s
```

```text
# 3. resume and bring the link back
ACE_EXT_FW_RESUME ACE=0
POST /printer/firmware_restart
```

### The two probes

```text
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2147497728
```
`2147497728` = `0x80003700` = packed op 0 (read register), reader 0,
a1 `0x37` (VersionReg). Expected: the response carries
**`"result":{"code":161}`** → `161 = 0xA1`, e.g.
`{"id":<N>,"result":{"code":161},"code":0,"msg":"success"}`.

> **Erratum in the task brief:** it quotes this probe as `2147484976`. That
> decimal is `0x80000530` and contradicts the brief's own formula
> `0x80000000 | (a1 & 0x3F) << 8 | ...`, which gives `0x80003700`. Use
> **2147497728** (or `0x80003700`).

```text
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2
```
Ordinary slot index 2. Expected: **normal slot behaviour** — the usual handler
response (`{"id":<N>,"result":{},"code":0,"msg":"success"}`, or `"FORBIDDEN"`
when the device is busy), no `0xA1`, no reboot; `ACE_EXT_RAW
METHOD=get_filament_info INDEX=2 FORCE=1 ACE=0` keeps reporting slot 2 exactly
as before.

### Regressions to record with the probes

```text
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0
    -> firmware "CV1.3.863" (unchanged flavour)
ACE_EXT_RAW METHOD=get_filament_info INDEX=0 FORCE=1 ACE=0
    -> slot 0 unchanged; a third-party tag still parses (rfid = 2, sku from tag)
```

### Rollback

Reflash the working image in exactly the same way
(`ACE_V1.3.863_cfw_uid.bin`, md5 `6148cfc52431fc235536c6d64b5334ef`, 113828 B,
crc16 `0xAE92`).

---

## 7. Open points / notes for Task 3 (P2)

* The probe hard-codes `reader=0` and `reg=0x37`; Task 4 decodes
  `op`/`reader`/`a1`/`a2` from the packed index (`artefacts_build_rc522_tunnel.py`
  already exposes `pack_index` / `is_packed_index` as the reference codec).
* P2 should confirm on hardware that `result.code` is the field the host reads
  back for a value, and re-check the `%d` ordering of the formatter (id first,
  value second), as implemented here.
* On any failure of the packed probe, roll back first and re-check
  `read_reg 0x0800E734` against the running image before changing the stub.
