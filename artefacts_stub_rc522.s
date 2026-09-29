/* artefacts_stub_rc522.s — ACE Pro (Gen 1) RC522 tunnel, full op set 0..8.
 *
 * Copyright (C) 2026 ACE-UID contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Hook site (unchanged from fix round 2)
 * --------------------------------------
 * Called from inside the `filament_recognition` handler at VA 0x080144F2
 * (function 0x080144D4; ACE command-table pair 0x08014674 -> name
 * "filament_recognition" at 0x08021508).  The hook replaces the handler
 * instruction `bl 0x08011D74` (file bytes fc 3f f7 fd).
 *
 * Why the hook is early (fix round 2)
 * -----------------------------------
 * The stock handler does NOT touch the RC522; its tail mutates the recognition
 * state machine / active slot (0x08014512..0x08014528) and only then replies at
 * 0x0801450A.  Hooking there kicked the stock NFC work on every tunnel op.
 * For a packed/tunnel index this stub replies and returns through the handler
 * epilogue 0x0801450E (`add sp,#0x14; pop {r4,r5,r6,r7,pc}`) without running
 * the tail.  Ordinary slot indices replay the displaced `bl 0x08011D74` as a
 * tail call, so stock behaviour is byte-for-byte unchanged.
 *
 * Reader ownership (fix round 3, completed in round 6)
 * ----------------------------------------------------
 * The recognition state machine runs in the main app task (`0x080137AA` ->
 * `0x08015C94` every ~100 ms) and drives the same bit-banged SPI.  While it is
 * active it soft-resets, flushes the FIFO and transceives, so a tunnel FIFO
 * pre-load is wiped and a tunnel transceive races it.  There is no firmware
 * lock to take (the firmware never needed one: the reader lives in a single
 * task).  The tunnel therefore suppresses the state machine:
 *
 *   every op   captures the recognition state byte (0x200071EC) at entry,
 *              drives it to 0 and puts the captured value back on EVERY reply
 *              path (success, 255 failures, unknown op, marker) -- round 6.
 *              The unknown-op and marker branches used to skip that restore,
 *              so the machine could be left paused (multiACE: status=busy
 *              until a power cycle);
 *   op 7  acquire: the explicit hold -- save the state, drive it to 0,
 *              osDelay(200 ms) to let an in-flight action finish, re-assert 0,
 *              and return the saved value.  Its reply restores 0, i.e. keeps
 *              the hold;
 *   op 8  release: restore the state passed in `a2` (usually the value op 7
 *              returned) -> 0.
 *
 * Reader / antenna bring-up (fix round 3, corrected in rounds 4/5/6)
 * -----------------------------------------------------------------
 * The firmware enables a channel with `0x0800E228(slot, 1)` and brings it up
 * with `0x0800F030()` (pin/table setup + chip reset pulses) + `0x0800E948`
 * (soft reset) + `0x0800EAEC(reader,3)` (antenna OR-3) + `0x0800E9EC(reader,0)`
 * (config) + `0x0800F4E4` (REQA/anticollision/SELECT).
 *
 * Round 4: `0x0800EAEC` reads TxControlReg (0x14) and returns early WITHOUT
 * writing when bits 0..1 already read as set, so relying on it can leave the
 * transmitter off.  op 6 does the bring-up itself.
 *
 * Round 6: the antenna write now uses the firmware's read-or-write semantics
 * (`set_bits`, `0x14 |= 3`) instead of `0x14 = 3`: other register bits are
 * preserved and bits 0..1 are always written.  If the select-only bring-up
 * fails, op 6 retries once through the firmware's full mode-1 entry
 * `0x0800E314(reader, 1)` -- the exact sequence the stock recognition uses on
 * a channel it can read.  See docs/notes-ops.md section 4b.
 *
 * Fix round 5 — the host-driven path (op 0/1 setup -> op 2 FIFO -> op 3)
 * ----------------------------------------------------------------------
 * The multiACE host (`ace_rc522.py`) drives a READ exactly like the stock
 * firmware's own read loop (0x08015B50 -> 0x0800E314(reader,1) then
 * 0x0800E3E0): after op 6 it does `_rc_setup_crc` (TXMODE 0x12 |= 0x80,
 * RXMODE 0x13 |= 0x80, BitFraming 0x0D = 0x00), pushes a **2-byte** frame
 * (`30 <page>`) into the FIFO and runs op 3.  The CRC is appended/checked by
 * the RC522 itself — the firmware's read path (0x0800E3E0) sets TxCRCEn and
 * RxCRCEn for exactly this frame shape.  Round 3 wrongly assumed a
 * host-supplied software CRC and had op 3 *clear* TxCRCEn, so the tag never
 * answered (empty FIFO, op 5 = 0).  op 3 now sets TxCRCEn (hardware CRC) and
 * RxCRCEn (verify + strip the reply CRC), and re-asserts the antenna bits
 * without any reset, so nothing destroys the host's pre-loaded FIFO.
 *
 * The bring-up is also ordered before the host's FIFO writes: op 1 writing
 * TxModeReg (0x12) — the first register write of the host's transceive
 * sequence — runs `full_bringup` first (the op-6 recipe).  Any chip
 * reset/init therefore happens before the frame is loaded, never after.
 * `full_bringup` is shared by op 6 and this trigger, so both take the round-6
 * fixes: `0x14 |= 3` (not `= 3`) and the mode-1 retry.
 *
 * Interface at the hook
 * ---------------------
 *   r0 = (scratch; the JSON parse result)
 *   r4 = response/output buffer pointer        (handler's 4th argument)
 *   r6 = JSON-RPC request id                   (handler's 1st argument)
 *   [sp, #52] = the full 32-bit `params.index` (the stub pushes 10 registers at
 *               entry; `bl` itself does not push — handler [sp,#12] + 40)
 *   lr = 0x080144F7 (return into the handler after the displaced call)
 *
 * Packed index (host contract, always sent SIGNED per R10)
 * --------------------------------------------------------
 *   index = 0x80000000 | (reader<<24) | (op<<16) | ((a1 & 0x3F)<<8) | (a2 & 0xFF)
 *
 * Branching
 * ---------
 *   bit 31 set                -> op 0..8 dispatch, then handler epilogue
 *   index == 0x7FFFFFFF       -> lossy unsigned fallback: fixed op0/reader0/
 *                                a1=0x37 (the signed form is canonical)
 *   index >= 0x10000, bit 31 clear -> MARKER (code 90, reachability only)
 *   index 0..3 / anything else -> replay `bl 0x08011D74` (stock, unchanged)
 *
 * Ops (result always in `result.code` via the CFW formatter 0x0801DC74)
 * -------------------------------------------------------------------
 *   0  read RC522 register          a1=reg                -> reg value
 *   1  write RC522 register         a1=reg, a2=value      -> 0
 *      (a write to TxModeReg 0x12 first runs `full_bringup`, so the reader
 *       is up before the host loads its frame)
 *   2  FIFO write byte              a1=index, a2=byte     -> 0
 *   3  PCD command                  a1=tx bytes, a2=cmd   -> ErrorReg
 *      (sets TxCRCEn/RxCRCEn + antenna bits first; never resets, so the
 *       host's pre-loaded FIFO frame survives)
 *   4  FIFO read byte               a1=index              -> FIFO byte
 *   5  received bits                a1=0                  -> count (bytes*8)
 *   6  reader enable + bring-up + SELECT                  -> 0 = card present,
 *      2 = collision, 0xFF = no card/error; a failed select retries once
 *      through the firmware's full mode-1 bring-up
 *   7  acquire reader (explicit hold; reply keeps state 0) -> saved state byte
 *   8  release reader (a2 = saved state)                  -> 0
 *   Every reply path (including the marker and an unknown op) restores the
 *   recognition state captured at entry.
 *
 * Reader selection (fix round 6, re-derived from the firmware)
 * ------------------------------------------------------------
 * The recognition dispatcher 0x08015C94 -> state-11 handler 0x080159E4 selects
 * a channel at 0x08015A52/0x08015A5C with `0x0800E228(4, 0)` then
 * `0x0800E228(slot, 1)`; the state-6 reader path (0x08015B56) then brings the
 * same index up with 0x0800E314.  Line/mask map (see select_reader and
 * docs/notes-ops.md §6): line 0..3 = PB10/PB11/PA14/PA13, `1` = LOW, and
 * descriptor index N (tables 0x20006240/0x200062A0) is the SPI channel whose
 * CS is PB12 for {0,2} and PC6 for {1,3}.  reader = slot = 0..3.
 *
 * CFW primitives (all Thumb):
 *   read_reg(reader, reg)       -> byte  0x0800E734 (thumb 0x0800E735)
 *   write_reg(reader, reg, v)   -> 1     0x0800E7C0 (thumb 0x0800E7C1)
 *   set_bits(reader, reg, b)    -> 1     0x0800E8E0 (thumb 0x0800E8E1)
 *   timer_setup(reader, ms)              0x0800E974 (thumb 0x0800E975)
 *   reader_enable(idx, on)               0x0800E228 (thumb 0x0800E229)
 *   reader_bringup(reader, 1)   -> 0     0x0800E314 (thumb 0x0800E315)
 *   all_reader_init()                    0x0800F030 (thumb 0x0800F031)
 *   state_get() / state_set(v)           0x08011E98 / 0x08011EA4 (+1)
 *   osDelay(ms)                          0x0800BF1C (thumb 0x0800BF1D)
 *   JSON reply formatter                 0x0801DC74 (thumb 0x0801DC75)
 * The RC522 register set used (see docs/notes-ops.md):
 *   0x01 CommandReg, 0x02 ComIEnReg, 0x04 ComIrqReg, 0x06 ErrorReg,
 *   0x09 FIFODataReg, 0x0A FIFOLevelReg, 0x0C ControlReg, 0x0D BitFramingReg,
 *   0x12 TxModeReg, 0x13 RxModeReg, 0x2A TModeReg, 0x37 VersionReg.
 *
 * Image ceiling
 * -------------
 * The IAP staging base is 0x08024000 and the app starts at 0x08008000, so the
 * whole applied image must be <= 0x1C000 = 114688 bytes.  The base is 113720 B,
 * leaving 968 B for the parser stub (108 B) plus this stub.  An oversized image
 * is accepted by `iap_upgrade` (code 0) but never commits; the builder asserts
 * MAX_IMAGE_BYTES (see artefacts_build_rc522_tunnel.py).
 */
    .syntax unified
    .cpu cortex-m4
    .thumb
    .text
    .global _start

    .equ    READ_REG,    0x0800E735
    .equ    WRITE_REG,   0x0800E7C1
    .equ    SET_BITS,    0x0800E8E1
    .equ    TIMER,       0x0800E975
    .equ    ENABLE,      0x0800E229     /* reader_enable(idx, on) */
    .equ    BRINGUP,     0x0800E315
    .equ    INIT_ALL,    0x0800F031     /* all-reader GPIO/antenna init */
    .equ    RESET,       0x0800E949     /* soft reset (CommandReg = 0x0F) */
    .equ    CONFIG,      0x0800E9ED     /* component config (mode 0) */
    .equ    STATE_GET,   0x08011E99
    .equ    STATE_SET,   0x08011EA5
    .equ    DELAY,       0x0800BF1D     /* osDelay(ms) */
    .equ    RESPOND,     0x0801DC75
    .equ    REPLAY,      0x08011D75     /* the displaced `bl 0x08011D74` */
    .equ    EPILOGUE,    0x0801450F     /* handler 0x0801450E, thumb */
    .equ    SENTINEL,    0x7FFFFFFF
    .equ    MARKER_MIN,  0x00010000
    .equ    SENT_IDX,    0x00003700     /* op0, reader0, a1=0x37, a2=0 */

    .equ    R_COMMAND,   0x01
    .equ    R_COMIEN,    0x02
    .equ    R_COMIRQ,    0x04
    .equ    R_ERROR,     0x06
    .equ    R_FIFODATA,  0x09
    .equ    R_FIFOLEVEL, 0x0A
    .equ    R_CONTROL,   0x0C
    .equ    R_BITFRAMING,0x0D
    .equ    R_TXMODE,    0x12
    .equ    R_RXMODE,    0x13
    .equ    R_TMODE,     0x2A
    .equ    PCD_TRANSCEIVE, 0x0C
    .equ    IRQ_MASK,    0x31           /* RxIRq|IdleIRq|TimerIRq */
    .equ    TIMEOUT_MS,  10             /* same timeout the firmware READ uses */
    .equ    WAIT_ITERS,  0xBB8          /* the firmware transceive's own bound */
    .equ    DRAIN_MS,    200            /* op7: let an in-flight action finish */

/* The prologue saves 10 registers; [sp,#36] holds the recognition state
 * captured at entry.  EVERY reply path puts that value back (fix round 6):
 * the unknown-op and marker branches used to skip the restore, which left the
 * ACE's recognition machine paused (multiACE: status=busy until a power
 * cycle).  op 7 (acquire) overwrites the saved slot with 0 so the reply keeps
 * the hold, and op 8 (release) overwrites it with `a2` -- both are then
 * restored by the same unconditional reply path. */
_start:
    push    {r3, r4, r5, r6, r7, r8, r9, r10, r11, lr}
    mov     r10, r4                 /* response buffer */
    mov     r11, r6                 /* request id */
    ldr     r12, [sp, #52]          /* params.index */
    lsrs    r4, r12, #31
    bne     packed
    ldr     r4, =SENTINEL
    cmp     r12, r4
    beq     sentinel
    ldr     r4, =MARKER_MIN
    cmp     r12, r4
    bhs     marker

    /* --- ordinary slot index: tail-call the displaced bl 0x08011D74 ------- */
    pop     {r3, r4, r5, r6, r7, r8, r9, r10, r11, lr}
    ldr     r12, =REPLAY
    bx      r12                     /* 0x08011D74 returns via lr = 0x080144F7 */

sentinel:
    ldr     r4, =SENT_IDX
    b       capture
marker:                             /* reachability marker: constant 90 */
    movs    r9, #90                 /* 0x5A */
    bl      state_get               /* marker also owns/pauses the machine ... */
    str     r0, [sp, #36]
    bl      pause_machine
    b       reply                   /* ... and restores it above */
packed:
    mov     r4, r12
capture:
    bl      state_get
    str     r0, [sp, #36]           /* capture the recognition state at entry */
    bl      pause_machine           /* take the reader off the stock NFC task */
unpack:
    ubfx    r5, r4, #16, #8         /* op */
    ubfx    r6, r4, #24, #2         /* reader */
    ubfx    r7, r4, #8,  #6         /* a1 */
    uxtb    r8, r4                  /* a2 */
    cmp     r5, #0
    beq     op_read
    cmp     r5, #1
    beq     op_write
    cmp     r5, #2
    beq     op_fifo_write
    cmp     r5, #3
    beq     op_pcd
    cmp     r5, #4
    beq     op_fifo_read
    cmp     r5, #5
    beq     op_rx_bits
    cmp     r5, #6
    beq     op_select
    cmp     r5, #7
    beq     op_acquire
    cmp     r5, #8
    beq     op_release
    movs    r9, #0xFF               /* unknown op: still restore the state */
    b       reply

op_read:                            /* op0: r = read_reg(reader, a1) */
    mov     r0, r6
    mov     r1, r7
    bl      rreg
    uxtb    r9, r0
    b       reply

op_write:                           /* op1: write_reg(reader, a1, a2) -> 0 */
    /* The host starts its transceive sequence by setting TxCRCEn in
     * TxModeReg (0x12) — `_rc_setup_crc` in ace_rc522.py.  Bring the reader
     * up on that first register write, BEFORE the host writes its FIFO
     * frame: the op-6 recipe (incl. its soft reset) must never run after the
     * frame exists.  Any other register write is passed through untouched. */
    cmp     r7, #R_TXMODE
    bne     ow_go
    bl      full_bringup            /* 0 / 2 / -1 ignored: the write must land */
ow_go:
    mov     r0, r6
    mov     r1, r7
    mov     r2, r8
    bl      wreg
    movs    r9, #0
    b       reply

op_fifo_write:                      /* op2: FIFODataReg (0x09) = a2 -> 0 */
    mov     r0, r6
    movs    r1, #R_FIFODATA
    mov     r2, r8
    bl      wreg
    movs    r9, #0
    b       reply

op_fifo_read:                       /* op4: FIFODataReg (0x09) -> byte */
    mov     r0, r6
    movs    r1, #R_FIFODATA
    bl      rreg
    uxtb    r9, r0
    b       reply

op_rx_bits:                         /* op5: received bits */
    mov     r0, r6
    bl      rx_bits
    uxtb    r9, r0
    b       reply

op_pcd:                             /* op3: trigger PCD command -> ErrorReg */
    mov     r0, r6
    mov     r1, r8                  /* command */
    bl      xcv_core
    uxtb    r9, r0
    b       reply

op_select:                          /* op6: full reader bring-up + SELECT */
    bl      full_bringup            /* shared with the op-1 TxMode trigger */
    uxtb    r9, r0
    b       reply

op_acquire:                         /* op7: return saved state, drain, hold 0 */
    movs    r0, #0
    bl      state_set
    movs    r0, #DRAIN_MS
    bl      delay                   /* let an in-flight action finish */
    movs    r0, #0
    bl      state_set
    ldr     r9, [sp, #36]           /* the state captured at entry */
    movs    r0, #0
    str     r0, [sp, #36]           /* keep the hold: reply restores 0 */
    uxtb    r9, r9
    b       reply

op_release:                         /* op8: restore state from a2 */
    mov     r0, r8
    bl      state_set
    str     r8, [sp, #36]           /* reply restores the same value again */
    movs    r9, #0
    b       reply

reply:
    ldr     r0, [sp, #36]           /* every reply path puts the stock */
    uxtb    r0, r0                  /* recognition state captured at entry */
    bl      state_set               /* back (marker/unknown-op included) */
    mov     r3, r9
    mov     r2, r11
    mov     r0, r10
    adr     r1, value_fmt
    ldr     r12, =RESPOND
    blx     r12
    pop     {r3, r4, r5, r6, r7, r8, r9, r10, r11, lr}   /* sp = handler local base */
    ldr     r12, =EPILOGUE
    bx      r12                     /* 0x0801450E: add sp,#0x14; pop {r4-r7,pc} */

/* --- firmware primitive wrappers ----------------------------------------- */

rreg:                               /* r0=reader, r1=reg -> r0=byte */
    ldr     r12, =READ_REG
    bx      r12

wreg:                               /* r0=reader, r1=reg, r2=value */
    ldr     r12, =WRITE_REG
    bx      r12

sbits:                              /* r0=reader, r1=reg, r2=bits -> set */
    ldr     r12, =SET_BITS
    bx      r12

timer:                              /* r0=reader, r1=timeout ms */
    ldr     r12, =TIMER
    bx      r12

enable:                             /* r0=idx, r1=on */
    ldr     r12, =ENABLE
    bx      r12

bringup:                            /* r0=reader, r1=1 -> 0 = card present */
    ldr     r12, =BRINGUP
    bx      r12

reset:                              /* r0=reader; soft reset */
    ldr     r12, =RESET
    bx      r12

config:                             /* r0=reader, r1=0; component config */
    ldr     r12, =CONFIG
    bx      r12

init_all:                           /* all-reader GPIO/antenna init, void */
    ldr     r12, =INIT_ALL
    bx      r12

state_get:                          /* -> r0 = recognition state byte */
    ldr     r12, =STATE_GET
    bx      r12

state_set:                          /* r0 = new recognition state */
    ldr     r12, =STATE_SET
    bx      r12

delay:                              /* r0 = ms */
    ldr     r12, =DELAY
    bx      r12

/* --- reader ownership ----------------------------------------------------- *
 * Drive the recognition state to 0 so the ~100 ms dispatcher (0x08015C94)
 * returns without touching the reader.  `reply` restores the value captured
 * at entry, so each op leaves the machine exactly as it found it; op 7 is the
 * explicit hold (its reply keeps 0).                                           */
pause_machine:
    push    {lr}
    movs    r0, #0
    bl      state_set
    pop     {pc}

/* --- reader/antenna selection --------------------------------------------- *
 * Re-derived for the Gen 1 four-channel hardware from the recognition
 * dispatcher 0x08015C94 -> state-11 handler 0x080159E4 -> 0x08015A52/
 * 0x08015A5C (`(4, 0)` then `(slot, 1)`), then the reader bring-up
 * 0x0800E314 (state 6: 0x08015B56 with the same index).
 *
 *   enable = 0x0800E228(idx, val), a jump table:
 *     idx 0..3: val==1 -> BRR (line LOW), val==0 -> BSRR (line HIGH);
 *               line 0 = PB10, 1 = PB11, 2 = PA14, 3 = PA13;
 *     idx 4:    val is a 4-bit mask, bit i -> line i (1 = LOW).
 *   (constants at 0x0800E30C/0x0800E310; 0x0800E1C4 configures the pins)
 *
 * The SPI side is the descriptor index passed to read_reg/write_reg
 * (tables 0x20006240/0x200062A0, entry = index); entries {0,2} share
 * CS = PB12 and {1,3} share CS = PC6 (chip reset pulses PC9 / PA8 come from
 * 0x0800EECC/0x0800EE60 inside 0x0800F030 = init_all).  So index N selects
 * the same physical channel for both the line and the SPI chip: reader =
 * slot = 0..3, exactly the semantics the packed index exposes.                 */
select_reader:
    push    {lr}
    movs    r0, #4
    movs    r1, #0
    bl      enable                  /* (4, 0): all selection lines HIGH */
    mov     r0, r6
    movs    r1, #1
    bl      enable                  /* (reader, 1): this channel LOW */
    pop     {pc}

/* --- full reader bring-up (op 6 and the op-1 TxMode trigger) -------------- *
 * The firmware's recognition step before a reader action (0x08015AC0:
 * 0x0800F030 first, then `0x0800E314(reader, 1)` = reset + antenna + config +
 * REQA/anticollision/SELECT).  Two deliberate differences, both fixing round 6:
 *
 *  1. the antenna bits are written unconditionally, but with the firmware's
 *     read-or-write semantics (`set_bits`, as 0x0800EAEC does): `0x14 |= 3`
 *     preserves any other bits the chip had.  A plain `0x14 = 3` would clear
 *     them; the firmware's own routine never does that.  Unlike 0x0800EAEC,
 *     this never returns early: bits 0..1 are always written.
 *  2. if the select-only entry fails, retry once through the firmware's full
 *     mode-1 bring-up (`0x0800E314(reader, 1)`) -- the exact sequence the
 *     stock recognition uses on a reader it can read.
 *
 * r6 = reader -> r0 = SELECT result (0 = card ACTIVE, 2 = collision,
 * 0xFF = no card / error).                                                      */
full_bringup:
    push    {lr}
    bl      init_all                /* 0x0800F030: pin/table setup + reset pulse */
    bl      select_reader           /* re-assert the slot after init_all */
    mov     r0, r6
    bl      reset                   /* 0x0800E948: soft reset */
    movs    r0, #2
    bl      delay
    mov     r0, r6
    movs    r1, #0x14
    movs    r2, #0x03
    bl      sbits                   /* TxControlReg |= 3, unconditionally */
    mov     r0, r6
    movs    r1, #0
    bl      config                  /* 0x0800E9EC(reader, 0) */
    movs    r0, #50
    bl      delay                   /* let the RF field come up */
    bl      select_reader           /* re-assert immediately before SELECT */
    mov     r0, r6
    movs    r1, #0
    bl      bringup                 /* 0x0800E314(reader, 0): REQA+anticoll+SELECT */
    cmp     r0, #0
    beq     fb_done
    mov     r0, r6
    movs    r1, #1
    bl      bringup                 /* retry: the firmware's full mode-1 path */
fb_done:
    pop     {pc}

/* --- op5: received bits --------------------------------------------------- *
 * count = FIFOLevel & 0x7F bytes; if ControlReg last-bits (0x07) is non-zero,
 * count = (bytes-1)*8 + lastbits, else bytes*8 (16 bytes -> 0x80).           */
rx_bits:                            /* r0=reader -> r0=count */
    push    {r4, r5, lr}
    mov     r5, r0
    movs    r1, #R_FIFOLEVEL
    bl      rreg
    ands    r0, r0, #0x7F
    lsls    r4, r0, #3
    mov     r0, r5
    movs    r1, #R_CONTROL
    bl      rreg
    ands    r0, r0, #0x07
    beq     rx_done
    subs    r4, r4, #8
    add     r4, r4, r0
rx_done:
    mov     r0, r4
    pop     {r4, r5, pc}

/* --- op3: PCD command core (mirrors the firmware transceive setup) -------- *
 * The host pre-loads the FIFO (op2) with the bare 2-byte frame (`30 <page>`)
 * and lets the RC522 append the CRC: the firmware's own read (0x0800E3E0)
 * sets TxCRCEn (0x12 |= 0x80) + RxCRCEn (0x13 |= 0x80) for exactly this frame
 * shape, so RxCRCEn makes the receiver verify and strip the tag's 2 CRC bytes
 * (16 data bytes -> op5 = 0x80).  The antenna bits are re-asserted here
 * (no reset, FIFO untouched) so op 3 does not depend on a prior op 6.  TModeReg
 * TAuto, ComIEnReg and the firmware's 10 ms timeout are programmed exactly as
 * 0x0800EB8C does.  The wait is bounded, so a dead field returns ErrorReg
 * instead of hanging.  r0=reader, r1=command -> r0=ErrorReg                      */
xcv_core:
    push    {r4, r5, r6, lr}
    mov     r4, r0
    mov     r5, r1
    bl      select_reader           /* re-assert the slot right before RF */
    mov     r0, r4
    movs    r1, #0x14
    movs    r2, #0x03
    bl      sbits                   /* TxControlReg |= 3: field on, no reset */
    mov     r0, r4
    movs    r1, #R_TXMODE
    movs    r2, #0x80
    bl      sbits                   /* TxCRCEn ON: the reader appends the CRC */
    mov     r0, r4
    movs    r1, #R_RXMODE
    movs    r2, #0x80
    bl      sbits                   /* RxCRCEn ON: verify + strip the reply CRC */
    mov     r0, r4
    movs    r1, #TIMEOUT_MS
    bl      timer
    mov     r0, r4
    movs    r1, #R_TMODE
    movs    r2, #0x80
    bl      sbits                   /* TModeReg |= TAuto */
    mov     r0, r4
    movs    r1, #R_COMIRQ
    movs    r2, #0x7F
    bl      wreg
    mov     r0, r4
    movs    r1, #R_COMIEN
    movs    r2, #0xF7
    bl      wreg                    /* ComIEnReg, as 0x0800EB8C does */
    mov     r0, r4
    movs    r1, #R_BITFRAMING
    bl      rreg
    ands    r0, r0, #0x07
    mov     r6, r0
    mov     r0, r4
    movs    r1, #R_COMMAND
    movs    r2, #0
    bl      wreg
    mov     r0, r4
    movs    r1, #R_COMMAND
    mov     r2, r5
    bl      wreg
    mov     r0, r4
    movs    r1, #R_BITFRAMING
    orr     r2, r6, #0x80
    bl      wreg
    ldr     r5, =WAIT_ITERS          /* bounded wait, never hangs */
xc_wait:
    mov     r0, r4
    movs    r1, #R_COMIRQ
    bl      rreg
    and     r0, r0, #IRQ_MASK
    bne     xc_done
    subs    r5, r5, #1
    bne     xc_wait
xc_done:
    mov     r0, r4
    movs    r1, #R_BITFRAMING
    movs    r2, #0
    bl      wreg
    mov     r0, r4
    movs    r1, #R_ERROR
    bl      rreg
    pop     {r4, r5, r6, pc}

    .align  2
value_fmt:
    .asciz  "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"ok\"}"
