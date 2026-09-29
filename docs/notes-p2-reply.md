<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 ACE-UID contributors -->

# Task 2 / probe P2 — the reply channel (`result.code`) and why P1 returned nothing

This is the offline deliverable of P2: the CFW reply formatter, the root cause
of the failed P1 probe, the corrected probe stub/image, and the commands the
controller runs. **No device was touched from this session.**

Base image: `ACE_V1.3.863_20260716.bin`, md5 `9f7b9a678a96caf98d6a08842d3ff971`,
113720 B. Working image: `ACE_V1.3.863_cfw_uid.bin`, md5
`6148cfc52431fc235536c6d64b5334ef`, 113828 B.

---

## 1. What the device showed for P1, and what it means

The controller flashed the P1 image and ran
`ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2147497728`.
The reply was `{"id":35,"result":{},"code":0,"msg":"success"}` — the **normal**
handler reply (`0x08021130` format), i.e. our stub took its *ordinary* branch and
the bit-31 test never fired.

That is not a missing reply field: the stub simply never saw bit 31 in
`params.index`. The cause is in the firmware's parameter parser.

## 2. Root cause: `params.index` is parsed by *signed* `strtol`

The handler (`0x080144D4`) parses its parameter through `0x0801863C`:

```
080144e0  add   r3, sp, #12        ; destination
080144e6  ldr   r2, [pc, #68]      ; 0x08021068 "params.index"
080144ee  bl    0x0801863c         ; json get-int
```

and `0x0801863C` converts the numeric token with the reentrant signed parser:

```
0x0801863C  json get-int
   -> 0x0801C2C6   movs r2,#10 ; movs r1,#0 ; b 0x0801CFD4
   -> 0x0801CFD4   strtol(str, NULL, 10)
   -> 0x0801CEE0   _strtol_r
```

In `_strtol_r` the positive overflow limit is built as:

```
0801cf4c  add.w ip, r6, #0x80000000    ; r6 = sign flag (0 pos / 1 neg)
0801cf50  add.w ip, ip, #0xFFFFFFFF    ; ip = 0x7FFFFFFF (pos) / 0x80000000 (neg)
...
0801cfa6  adds  r3, r2, #1             ; r2 = 0xFFFFFFFF on overflow
0801cfaa  movs  r3, #34                ; ERANGE
0801cfac  str.w r3, [lr]               ; errno
0801cfb0  mov   r0, ip                 ; return LONG_MAX
```

So **every host value ≥ 2³¹ is clamped to `LONG_MAX = 0x7FFFFFFF`**, with the
payload bits (reader/op/a1/a2) irrecoverably lost. `0x0801863C` stores that
result unconditionally, so `[sp,#12]` = `0x7FFFFFFF` for our probe, bit 31 is
clear, and the stub fell through to the ordinary branch.

This parser is not specific to `filament_recognition`: the same `0x0801863C`
getter has 60+ call sites (all ACE commands that take an integer parameter), so
the clamp is universal.

Consequence for the tunnel contract: the multiACE host sends
`packed = 0x80000000 | …` as a **positive** decimal; on this firmware it arrives
as `0x7FFFFFFF`. To keep the full payload the host must send the same 32-bit
pattern as a **signed** value (e.g. `INDEX=-2147469568`, two's complement of
`0x80003700`), which `strtol` preserves exactly. The probe stub accepts both
forms.

## 3. The CFW reply formatter (stock 0x080159CC equivalent)

The call the hook replaced, `0x0801DC74`, **is** the CFW reply formatter — the
analogue of the stock image's `0x080159CC`.

```
0x0801DC74  respond(void *dst, const char *fmt, vararg1, vararg2, ...)
   thumb entry 0x0801DC75
```

* `r0` = destination buffer; `r1` = `printf`-style format; `r2`, `r3`, … =
  varargs (vargs are read from the caller's spilled `r1..r3`, in order).
* It builds a small stream descriptor at its `[sp,#8]` (dst at field 0),
  calls the printf engine **`0x0801FFF8`** (`r0 = *0x2000A804`, `r1 = &stream`,
  `r2 = fmt`, `r3 = va_list = &saved r2`), and NUL-terminates the result.
* `0x0801F470` is the engine's fallback allocator for long output.
* It returns nothing the caller uses; the caller's buffer holds the JSON.

Cross-check (the same function builds other commands' `result` fields): the
`get_filament_info` handler calls it at `0x080143AA` as
`respond(&local, fmt, arg)` and then folds the formatted text into the reply at
`0x080185F0` — confirming the `(dst, fmt, …)` shape.

In the `filament_recognition` handler the destination is the handler's 4th
argument, which is live in `r0` at the hook, and the handler's own format is
`0x08021130` = `{"id":%d,"result":{},"code":0,"msg":"%s"}`.

## 4. The corrected stub (`artefacts_stub_probe.s`, 136 B)

At the hook (VA `0x0801450A`, `r0`=response buffer, `r1`=fmt, `r2`=request id,
`r3`=msg, and `params.index` at `[sp,#12]`):

1. `index & 0x80000000` set → **packed request with payload**: decode
   `reader = (index >> 24) & 3`, `a1 = (index >> 8) & 0x3F`.
2. `index == 0x7FFFFFFF` (the signed-overflow sentinel produced by
   `INDEX=2147497728`) → **clamped request, payload lost**: use the fixed P2
   probe `reader = 0`, `a1 = 0x37` (VersionReg).
3. anything else → replay the displaced `bl 0x0801DC74` with `r0..r3` untouched
   (byte-for-byte the original handler behaviour).

For a tunnel request it calls `read_reg(reader, a1)` (`0x0800E734`, thumb
`0x0800E735` — the primitive the firmware uses at `0x0800E8CC` to read
VersionReg 0x37), then calls the formatter with our own format string kept in
the appended tail:

```
probe_fmt: "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"ok\"}"
```

passing `respond(response_buffer, probe_fmt, request_id, register_value)`, so the
byte lands in **`result.code`** — exactly where the multiACE host reads it
(`resp['result']['code'] & 0xFF`). Expected for the probe: `161` (`0xA1`).

### Why calling the formatter from the hook is safe

* The hook replaces a 4-byte `bl` with another 4-byte `bl`; no instruction is
  split. The stub is a normal callee: it saves `r0`/`r2`/`lr`, touches only
  `r12` (scratch) and `r4`/`r5` (restored by the handler's own epilogue), and
  returns into the handler epilogue at `0x0801450E`, which unwinds the handler
  frame and returns to the dispatcher as usual.
* It passes the formatter the **same buffer** the handler passes
  (`r0` at the hook), so the dispatcher transmits our JSON exactly as it would
  transmit the handler's.
* The ordinary path never runs the formatter twice: the stub replays the
  original call and returns; no additional response is emitted.
* The appended stub is in the free image tail; the frozen parser hook and its
  108-byte stub are byte-identical to the working image.

### Known benign side effect (unchanged from P1)

A tunnel probe still reaches the handler's tail, so the success path also runs
`set_active_slot(index & 0xFF)`. For the clamped probe that is `0xFF`; for a
packed probe it is `a2`. Harmless for a debug probe; Task 4/5 should mask
bit 31 (or hook earlier) before this becomes a production path.

## 5. Probe image

Built with:

```
python3 artefacts_build_rc522_tunnel.py ACE_V1.3.863_20260716.bin /tmp/probe-p2.bin \
    --hook-fixed --hook 0x0801450A --stub artefacts_stub_probe.s --version 1.3.863
```

Result (pinned by `tests/test_probe_hook.py`):

```
md5    9b1d65ad8cd1edc0996581a6f02e7205
size   113964
crc16  0x6575

parser hook 0x08016B9A  f8931036 -> 0df04df8   (frozen, unchanged; halfword)
probe  hook 0x0801450A  f009fbb3 -> f00ffbcb   (halfword)
                        09f0b3fb -> 0ff0cbfb   (file order) = bl 0x08023CA4
parser stub 108 B at VA 0x08023C38
probe  stub 136 B at VA 0x08023CA4
```

Changed byte ranges **vs the base** `ACE_V1.3.863_20260716.bin` (250 B total):

```
0x0801450A..0x0801450A   (1 B)   probe hook, byte 0
0x0801450C..0x0801450C   (1 B)   probe hook, byte 2  (bytes 1,3 unchanged)
0x08016B9A..0x08016B9D   (4 B)   frozen parser hook
0x08023C38..0x08023D2B   (244 B) appended tail = 108 B parser stub + 136 B probe stub
```

Changed bytes **vs the working** `ACE_V1.3.863_cfw_uid.bin` (138 B total):

```
0x0801450A..0x0801450A   (1 B)
0x0801450C..0x0801450C   (1 B)
0x08023CA4..0x08023D2B   (136 B) probe stub
```

Tail budget: the probe adds 136 B to the frozen 108 B; **832 B** of the 968 B
allowance remain.

## 6. Commands for the controller (live printer)

Replace `<printer>` with the printer host. No host edits are needed: the value
arrives in `result.code`.

```bash
rsync -av /tmp/probe-p2.bin <printer>:/tmp/probe-p2.bin
rsync -av ace_flash.py      <printer>:/tmp/ace_flash.py
ssh <printer> 'md5sum /tmp/probe-p2.bin'    # expect 9b1d65ad8cd1edc0996581a6f02e7205
```

```text
ACE_EXT_RAW METHOD=drying_stop FORCE=1 ACE=0
ACE_EXT_FW_RELEASE ACE=0
```

```bash
ssh <printer> 'python3 /tmp/ace_flash.py \
  /dev/serial/by-path/platform-fed00000.usb-usb-0:1.3.3:1.0 flash /tmp/probe-p2.bin'
# expect: image 113964 bytes, crc16=0x6575 ; iap_upgrade ok ; sent 113964 bytes in ~19s
```

```text
ACE_EXT_FW_RESUME ACE=0
POST /printer/firmware_restart
```

### The probe

```text
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2147497728
```
Expected:
**`{"id":<N>,"result":{"code":161},"msg":"ok"}`** — `161 = 0xA1` (VersionReg).

This value is clamped by the firmware to `0x7FFFFFFF`; the stub recognises the
sentinel and runs the fixed VersionReg probe. The same probe with the full
payload preserved:

```text
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=-2147469568
```
(=`0x80003700` as signed 32-bit) → same expected **`result.code = 161`**.

Ordinary index untouched:

```text
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2
```
→ the normal handler reply (no `result.code`), no reboot;
`ACE_EXT_RAW METHOD=get_filament_info INDEX=2 FORCE=1 ACE=0` unchanged.

### Regressions to record

```text
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0            -> firmware "CV1.3.863"
ACE_EXT_RAW METHOD=get_filament_info INDEX=0 FORCE=1 ACE=0 -> third-party tag still parses (rfid = 2)
```

### Rollback

Reflash `ACE_V1.3.863_cfw_uid.bin`
(md5 `6148cfc52431fc235536c6d64b5334ef`, 113828 B, crc16 `0xAE92`).

## 7. Notes for Task 3/4/5

* Task 4's host packing must send the packed index as the **signed** 32-bit value
  (`packed - 2³²` when bit 31 is set), otherwise the firmware clamps it to
  `0x7FFFFFFF` and the payload is lost. `as_signed32()` in the builder is the
  reference conversion.
* The stub currently implements op 0 only (`read_reg`). Task 4/5 must add ops
  1–6; `tunnel_probe_args()` is the reference decoder for reader/a1.
* If a future firmware build changes the parameter parser to unsigned, the
  `0x7FFFFFFF` sentinel path becomes dead but harmless; the bit-31 path is the
  forward-compatible one.

---

## 8. P2b — reachability marker (diagnostic image)

### 8.1 Device evidence, restated

P2 image (md5 `9b1d65ad8cd1edc0996581a6f02e7205`) on the device:

| probe | device answer |
|---|---|
| `INDEX=2147497728` (`0x80003700`, clamped by strtol) | `{"id":11,"result":{},"code":0,"msg":"success"}` |
| `INDEX=-2147469568` (`0x80003700` signed) | `{"id":16,"result":{},"code":0,"msg":"FORBIDDEN"}` |
| `INDEX=2` | success |
| `get_info` / tags / `get_filament_info` | healthy (`CV1.3.863`) |

Key point: the signed probe has bit 31 set, so the P2 stub's `value` branch
should have fired and returned `result.code=161`. It returned the stock
`FORBIDDEN` reply instead. That means, on that invocation, the P2 tunnel branch
did **not** execute — consistent with either

* **(A)** the hook at `0x0801450A` is not executed on this path / the flashed
  image is not the one running, or
* **(B)** the stub runs but reads the wrong `index`/writes the wrong buffer.

Both produce identical wire output, so a marker is required.

### 8.2 Register/stack evidence (what must be true if the handler runs)

* The handler for the `filament_recognition` name pointer `0x08021508` is the
  table pair at `0x08014674`: handler thumb-pointer `0x080144D5` → function
  `0x080144D4`. Verified again from the registration site `0x08014616`-`0x08014626`
  (`register(name=0x08021508, handler=0x080144D5, flag=0)`).
* `0x0801450A` is the handler's **only** reply emitter and both exits converge on
  it (`FORBIDDEN` falls through from `0x08014502`; the success path branches back
  to `0x08014504`). If the handler runs and emits its observed reply, the call at
  `0x0801450A` is what emitted it, so a live hook there must run.
* Reply arguments at the hook: `r0` = response buffer (handler 4th arg; the same
  buffer the handler's stock reply lands in — proven because that stock reply is
  what the wire shows), `r1` = format `0x08021130`, `r2` = request id (handler
  1st arg), `r3` = message string.
* `params.index` is parsed into handler `[sp,#12]` (32-bit LE). On Cortex-M
  `bl` does **not** push to the stack, so the stub is entered with the handler's
  SP and the word is at stub `[sp,#12]` (handler frame is `push {r4..r7,lr}` =
  20 B then `sub sp,#20`). The success path reads it as `ldrb.w r7,[sp,#12]` at
  `0x08014512`.  **Correction:** the P2 stub read `[sp,#16]`, the adjacent
  uninitialised local; the P2b marker proved it (§8.4) and §9 fixes it.
* The parser is the **signed** `strtol` (`0x0801863C → 0x0801C2C6 →
  _strtol_r 0x0801CEE0`): inside `_strtol_r` the accumulator returns in `r0`;
  `0x0801863C` stores it with `str r0,[r1,#0]` at `0x08018672` where `r1` is the
  handler's `[sp,#12]`. So the value is in a **register only inside the shared
  helper** — by the time it is back in `filament_recognition` (at `0x080144F2`)
  it lives in the stack slot, not a register.

### 8.3 The marker image (`artefacts_stub_marker.s`)

Called from the same hook `0x0801450A`; decision is purely on the 32-bit value
at `[sp,#16]` — which was the bug: it should have been `[sp,#12]` (§9), so the
marker fired for every index and proved only that the hook executes.

| index | branch | reply |
|---|---|---|
| bit 31 set | `value` | `read_reg(reader,a1)` → `{"id":%d,"result":{"code":%d},"msg":"ok"}` |
| else `index >= 4` | `marker` | `{"id":%d,"result":{"code":90},"msg":"marker"}` (no hardware access) |
| else `0..3` | `normal` | replay `bl 0x0801DC74` unchanged (slots behave exactly as stock) |

The marker branch touches no hardware, so a `90` proves the hook executed and
the reply reached the wire. The image is also built with **version
`CV1.3.864`** (one version byte changed at VA `0x08020AE8`) so `get_info`
independently proves which image is running.

Identity:

```
md5    44e7c01c98f35a2b4e4fb826ce82cce8
size   114016
crc16  0x4D45
parser hook 0x08016B9A  f8931036 -> 0df04df8   (frozen)
probe  hook 0x0801450A  f009fbb3 -> f00ffbcb   (halfword) / 09f0b3fb -> 0ff0cbfb (file)
version     0x08020AE8  33 -> 34               "CV1.3.863" -> "CV1.3.864"
marker stub 188 B at VA 0x08023CA4
changed vs base (303 B): 0x0801450A, 0x0801450C, 0x08016B9A..0x08016B9D,
            0x08020AE8, 0x08023C38..0x08023D5F (296 B tail)
```

```
python3 artefacts_build_rc522_tunnel.py ACE_V1.3.863_20260716.bin /tmp/probe-marker.bin \
    --hook-fixed --hook 0x0801450A --stub artefacts_stub_marker.s --version 1.3.864
```

### 8.4 Expected verdicts

```
get_info  -> "firmware":"CV1.3.864"     image active
get_info  -> "firmware":"CV1.3.863"     image NOT active -> reflash (neither A nor B proven)
```

| probe | marker image answer | meaning |
|---|---|---|
| `INDEX=1000000` (`0xF4240`, no clamp) | `code=90` | hook executes, index slot correct, reply reaches the wire → the P2 failure is in the tunnel condition, not the hook |
| `INDEX=1000000` | stock reply | hook does not execute → **(A)** (if `get_info` shows 864) |
| `INDEX=2147497728` | `code=90` | clamped value is `>= 4` (i.e. the long-value clamp reached the hook) |
| `INDEX=2147497728` | stock reply | clamped value is `< 4` (e.g. 0) — host must send the signed form |
| `INDEX=-2147469568` | `code=161` | bit-31 payload path, `read_reg(0x37)` and the reply channel all work → the fix for the real tunnel is the signed host encoding |
| `INDEX=2` | stock success | ordinary slots untouched |

Interpretation rule (per the task): a `90` on the packed probe ⇒ the hook fires
and the earlier failure was in the tunnel condition / reply content (B); no `90`
with `get_info = CV1.3.864` ⇒ (A).

### 8.5 Fallback site if (A)

If the marker never appears while `get_info` proves the image is active, the
reply we see cannot come from `0x080144D4` and the handler table must be
re-checked at runtime. The next candidate inside the same handler is the first
instruction after the parse:

```
VA 0x080144F2   bl 0x08011D74        (file fdf73ffc, halfword f7fdfc3f)
   -> parsed 32-bit index is at handler [sp,#12] == stub [sp,#12]
   -> r0 is the parse-success flag, so a stub here must replay `bl 0x08011D74`
      on the ordinary path, and on a tunnel path must reply and then unwind the
      handler frame straight to its epilogue 0x0801450E (`add sp,#20;
      pop {r4-r7,pc}`), otherwise the stock reply at 0x0801450A would overwrite.
```

A hook strictly *inside* `strtol` (`_strtol_r` returns the value in `r0` at
`0x0801CEE0`, stored at `0x08018672`) is shared by all 60+ integer parameters
and is therefore not usable per command.

### 8.6 Controller commands

```bash
rsync -av /tmp/probe-marker.bin <printer>:/tmp/probe-marker.bin
rsync -av ace_flash.py          <printer>:/tmp/ace_flash.py
ssh <printer> 'md5sum /tmp/probe-marker.bin'   # 44e7c01c98f35a2b4e4fb826ce82cce8
ACE_EXT_RAW METHOD=drying_stop FORCE=1 ACE=0
ACE_EXT_FW_RELEASE ACE=0
ssh <printer> 'python3 /tmp/ace_flash.py /dev/serial/by-path/platform-fed00000.usb-usb-0:1.3.3:1.0 flash /tmp/probe-marker.bin'
ACE_EXT_FW_RESUME ACE=0
POST /printer/firmware_restart
```

```text
# 0. image activity: MUST be CV1.3.864
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0

# 1. control reachability (no clamp involved)
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=1000000
    # expect {"id":<N>,"result":{"code":90},"msg":"marker"}

# 2. packed probe (the task's marker case)
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2147497728
    # expect code=90 -> hook fires;  stock reply -> (A)/clamped<4

# 3. value case (signed form, full payload)
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=-2147469568
    # expect {"id":<N>,"result":{"code":161},"msg":"ok"}

# 4. ordinary slot unchanged
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2
    # expect the stock success reply
```

Rollback: reflash `ACE_V1.3.863_cfw_uid.bin` as usual.

---

## 9. P3 fix — the frame bug and the corrected value image

### 9.1 Root cause: `bl` does not push on Cortex-M

The P2/P2b stubs read `params.index` at `[sp,#16]`, assuming the `bl` at
`0x0801450A` pushed a 4-byte return address. **It does not.** On ARM Cortex-M
`BL` only writes LR; the callee is entered with the *caller's* SP. The stock
tunnel stub in this repository confirms the convention (`push {lr}` then
`[sp,#8]` == entry `[sp,#4]`, `artefacts_stub_tunnel5.s`).

Frame, exactly:

```
handler entry S
0x080144D4  push {r4,r5,r6,r7,lr}      -> sp = S-20
0x080144D8  sub  sp,#20                -> sp = S-40      (local base = H)
0x080144EC  str  r5,[sp,#12]           -> zero H+12
0x080144EE  bl   0x0801863C            -> parse
              ... _strtol_r 0x0801CEE0 ...
0x08018672  str  r0,[r1,#0]  (r1=H+12) -> params.index = H+12  (32-bit LE)
0x0801450A  bl   <stub>                -> sp stays H (BL does not push)
```

So in the stub the index is at **`[sp,#12]`** (== handler `H+12`). The value at
`[sp,#16]` is `H+16`, the adjacent local of the handler frame: it is never
initialised by this path (only `H+12` is zeroed and written), so on the device
it held a stale value `>= 4` with bit 31 clear. That is why every probe —
`INDEX=2` included — wrongly took the marker branch and why the P2 value never
appeared.

(The signed-`strtol` provenance is unchanged: a host value ≥ 2³¹ is clamped to
`LONG_MAX = 0x7FFFFFFF` by `_strtol_r`; the parsed value is only in a register
inside the shared helper `0x0801863C`, and back in `filament_recognition` it
lives in the stack slot `H+12`.)

### 9.2 Choice: keep the hook at 0x0801450A

The parsed value **is** live at the hook (at `[sp,#12]`), so no hook move or
second scratch-RAM hook is needed: **one patch point, one stub offset fix**.
Risk: none beyond the offset itself — the hook site, the reply mechanism and the
ordinary path are already proven on hardware (P2b marker image).

### 9.3 Stub decisions (`artefacts_stub_probe.s`, 208 B)

| index | branch | reply |
|---|---|---|
| bit 31 set (CORRECT host form, e.g. `-2147469568` = `0x80003700`) | `value` | decode `reader=(i>>24)&3`, `a1=(i>>8)&0x3F`; `read_reg(reader,a1)`; `{"id":%d,"result":{"code":%d},"msg":"ok"}` |
| `== 0x7FFFFFFF` (UNSIGNED host form `2147497728`, payload clamped away) | `sentinel` | lossy fallback mapped to reader 0 / op 0 / a1 `0x37` → `result.code` = 161 |
| `>= 0x10000` bit 31 clear | `marker` | `{"id":%d,"result":{"code":90},"msg":"marker"}` (reachability only, off the normal path) |
| `0..3` (and anything else) | `normal` | replay `bl 0x0801DC74` unchanged (stock) |

**Which host form is correct:** the **signed** form. `INDEX=-2147469568` keeps
reader/op/a1/a2. The unsigned `INDEX=2147497728` is only a fallback: the firmware
clamps it, so the stub can answer only the fixed op0/reader0/a1=0x37 probe.
Task 4/5 must send the signed form (`as_signed32()` in the builder).

### 9.4 Image

```
python3 artefacts_build_rc522_tunnel.py ACE_V1.3.863_20260716.bin /tmp/probe-value.bin \
    --hook-fixed --hook 0x0801450A --stub artefacts_stub_probe.s --version 1.3.865
```

```
md5    a6d9230ae58ecc2370c42169e2b73f3e
size   114036
crc16  0xBE32
version CV1.3.865 (byte 0x08020AE8)   -> get_info proves the image is active
probe stub 208 B at VA 0x08023CA4 (tail 316 B)
changed vs base (323 B): 0x0801450A, 0x0801450C, 0x08016B9A..0x08016B9D,
            0x08020AE8, 0x08023C38..0x08023D73
changed vs working (211 B): 0x0801450A, 0x0801450C, 0x08020AE8,
            0x08023CA4..0x08023D73
```

The frozen parser hook and parser stub remain byte-identical to the working
image; `[sp,#12]` is the first instruction of the stub (`dd f8 0c c0`).

### 9.5 Expected outputs

| command | expected |
|---|---|
| `ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0` | `"firmware":"CV1.3.865"` |
| `ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=-2147469568` | `{"id":<N>,"result":{"code":161},"msg":"ok"}` |
| `ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2147497728` | `{"id":<N>,"result":{"code":161},"msg":"ok"}` (lossy fallback) |
| `ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=1000000` | `{"id":<N>,"result":{"code":90},"msg":"marker"}` (reachability only) |
| `ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2` | `{"id":<N>,"result":{},"code":0,"msg":"success"}` (or `"FORBIDDEN"` when busy) — stock, no code |
| `ACE_EXT_RAW METHOD=get_filament_info INDEX=0 FORCE=1 ACE=0` | unchanged: the slot-0 tag record (e.g. third-party `sku`/`brand`/`type`/`color`) |

### 9.6 Controller commands

```bash
rsync -av /tmp/probe-value.bin <printer>:/tmp/probe-value.bin
rsync -av ace_flash.py         <printer>:/tmp/ace_flash.py
ssh <printer> 'md5sum /tmp/probe-value.bin'   # a6d9230ae58ecc2370c42169e2b73f3e
ACE_EXT_RAW METHOD=drying_stop FORCE=1 ACE=0
ACE_EXT_FW_RELEASE ACE=0
ssh <printer> 'python3 /tmp/ace_flash.py /dev/serial/by-path/platform-fed00000.usb-usb-0:1.3.3:1.0 flash /tmp/probe-value.bin'
ACE_EXT_FW_RESUME ACE=0
POST /printer/firmware_restart
```

```text
ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0                    # must be CV1.3.865
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=-2147469568   # expect code=161
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2147497728    # expect code=161
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=1000000       # expect code=90 (marker)
ACE_EXT_RAW METHOD=filament_recognition FORCE=1 ACE=0 INDEX=2             # stock, no code
ACE_EXT_RAW METHOD=get_filament_info INDEX=0 FORCE=1 ACE=0                # unchanged
```

Rollback: reflash `ACE_V1.3.863_cfw_uid.bin`
(md5 `6148cfc52431fc235536c6d64b5334ef`, 113828 B, crc16 `0xAE92`).

---

## 10. Host probe tool, codec helpers and the signed packing (R10)

### 10.1 `probe/probe_tunnel.py`

The plan-mandated host driver.  It reuses the frame codec from `ace_flash.py`
(`FF AA | len(u16 LE) | payload | crc16(MCRF4XX) | FE`, JSON-RPC payload) and
issues the tunnel request through the `filament_recognition` carrier, with a
light `get_status` keepalive because the unit self-resets after ~3.3 s idle.

```python
from probe_tunnel import TunnelProbe          # probe/ on sys.path
p = TunnelProbe("/dev/serial/by-path/platform-fed00000.usb-usb-0:1.3.3:1.0", idx=0)
r = p.send(op=0, a1=0x37)                     # Reply(field, value, raw)
assert r.field == "code" and r.value == 0xA1  # 161
p.close()
```

* `send(op, a1=0, a2=0, reader=0) -> Reply(field, value, raw)` builds
  `packed = 0x80000000 | (reader<<24) | (op<<16) | ((a1&0x3F)<<8) | (a2&0xFF)`
  and sends it in the **signed** form (`packed - 2**32` when bit 31 is set,
  **R10**); it parses the answer from `result.code & 0xFF` and returns it next
  to the raw reply.
* `send_packed(packed)` sends an already-packed value (also signed per R10).
* `read_reg(reg, reader=0)` is the op-0 convenience.
* CLI:

```bash
python3 probe/probe_tunnel.py probe    --dev <port> [--idx N] [--packed 0x80003700]
python3 probe/probe_tunnel.py probe    --dev <port> [--op O --a1 0x37 --a2 A --reader R]
python3 probe/probe_tunnel.py read-reg --dev <port> --reg 0x37 [--reader R]
```

Expected for the delivered value image (CV1.3.865):

| command | expected `Reply` |
|---|---|
| `read-reg --reg 0x37` | `field="code", value=161` |
| `probe --packed 0x80003700` | `field="code", value=161` (signed form) |
| `probe --packed 0x7FFFFFFF` | `field="code", value=161` (lossy unsigned fallback) |
| `probe --packed 0x100000` | `field="code", value=90` (marker, reachability only) |

The tool was never run against hardware from this session.  Offline tests live
in `tests/test_probe_tunnel.py` and use a fake transport (no serial I/O).

### 10.2 Codec helpers and drift risk

`pack_index`, `as_signed32`, `is_packed_index`, `is_tunnel_index`,
`tunnel_probe_args`, `marker_decision` and `stub_branch` live in
`artefacts_build_rc522_tunnel.py` on purpose: the probe tool and the offline
tests import them, and Task 4 consumes them.  There are **two** decision
mirrors:

* `marker_decision` describes the historical P2b marker stub
  (`artefacts_stub_marker.s`, `[sp,#16]` bug preserved, md5-pinned);
* `stub_branch` describes the delivered stub (`artefacts_stub_probe.s`,
  `[sp,#12]`).

They are Python mirrors of the Thumb branch logic, so they can silently drift
from the `.s` sources.  Keep them in sync when a stub changes (the tests assert
the current behaviour), and prefer a single shared decoder once Task 4 settles
the final op set.
