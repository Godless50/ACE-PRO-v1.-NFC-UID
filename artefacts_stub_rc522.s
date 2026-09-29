/* artefacts_stub_rc522.s — ACE Pro (Gen 1) RC522 tunnel, full op set 0..8.
 *
 * Copyright (C) 2026 ACE-UID contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Fix round 7 — the RX path is ours, with a per-stage diagnostic record
 * --------------------------------------------------------------------
 * Live evidence with round 6 (CV1.3.871) on the unit whose tag faces antenna 3
 * (spool 4): `probe_ntag.py select-scan` showed
 *
 *   reader 3: SELECT code(s) [255] | stages: ComIrqReg=0x66, ErrorReg=0x00,
 *                                    FIFOLevelReg=0x00
 *
 * 0x66 = TxIRq|RxIRq|LoAlertIRq|ErrIRq: a response DID arrive (RxIRq) and an
 * error was flagged (ErrIRq), yet the firmware SELECT helper 0x0800F45C
 * reported failure and the FIFO was empty.  The firmware's transceive core
 * clears ErrorReg by writing 0xF7 on exactly one path (0x0800EE00, the CRCErr
 * path) -- so the last exchange saw a CRC error on a response and the routine
 * drained/discarded it.  The stock recognition path succeeds on this unit
 * because its state machine (0x08015AC0) retries the whole sequence with
 * ~100 ms of field settling between attempts; round 6 did one attempt per
 * entry and gave up.
 *
 * Round 7 therefore replaces the firmware SELECT with our own REQA /
 * anticollision / SELECT and owns every step that was invisible before:
 *
 *   1. REQA is sent with BitFramingReg = 0x07 (7-bit last byte, no CRC) and
 *      the ATQA is read out as a full two-byte frame; the next frame writes
 *      BitFramingReg = 0 explicitly (the 7 -> 0 transition the firmware hid).
 *   2. The IRQ is polled *before* the FIFO is touched (ComIrqReg & 0x31,
 *      bounded to the firmware's own 0xBB8 iterations) and the IRQ flags are
 *      never cleared between the wait and the readout: ComIrqReg, ErrorReg and
 *      FIFOLevelReg are sampled first, then the FIFO is drained.  Clearing
 *      ComIrqReg before reading the FIFO is the classic way to lose a frame.
 *   3. A response is accepted whenever bytes are present, even if ErrIRq /
 *      CRCErr / TimerIRq also fired: a tag only answers a frame it accepted,
 *      and the SELECT SAK byte is what proves the ACTIVE state.  The error
 *      flags stay in the diagnostic record instead of meaning "no card".
 *   4. The whole sequence is retried (`SELECT_TRIES` attempts, each with the
 *      firmware's own bring-up order reset -> antenna bits -> 100 ms ->
 *      component config -> 3 ms), because the stock path retries too.
 *   5. Every stage's raw bytes and registers are left in a 54-byte diagnostic
 *      record in the RC522 FIFO (op 0 reads FIFOLevelReg, op 4 pops the
 *      bytes).  `probe_ntag.py select-scan` prints it; the acceptance shape is
 *      a plausible ATQA (2 B), a 5-byte UID from CL1 anticollision, and
 *      SELECT -> 0 on the reader whose antenna faces the tag.
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
 *              path (success, 255 failures, unknown op, marker).
 *   op 7  acquire: the explicit hold -- save the state, drive it to 0,
 *              osDelay(200 ms) to let an in-flight action finish, re-assert 0,
 *              and return the saved value.  Its reply restores 0, i.e. keeps
 *              the hold;
 *   op 8  release: restore the state passed in `a2` (usually the value op 7
 *              returned) -> 0.
 *
 * Reader / antenna bring-up (fix round 3, corrected in rounds 4/5/6, mirrored
 * from the firmware's own mode-1 entry 0x0800E314(reader, 1) in round 7)
 * -------------------------------------------------------------------------
 * The firmware brings a channel up with a soft reset (0x0800E948), the antenna
 * bits (0x0800EAEC(reader, 3), 100 ms on both paths) and the component config
 * (0x0800E9EC(reader, 0)).  The stub does the same before every attempt, with
 * two deliberate differences:
 *
 *  - the antenna bits are written unconditionally with the firmware's own
 *    read-or-write semantics (`set_bits`, `0x14 |= 3`).  0x0800EAEC returns
 *    early without writing when bits 0..1 already read as set, so relying on it
 *    can leave the transmitter off (round 4);
 *  - the order is reset -> antenna bits -> 100 ms -> config -> 3 ms, exactly
 *    the firmware's own order.
 *
 * Interface at the hook
 * ---------------------
 *   r0 = (scratch; the JSON parse result)
 *   r4 = response/output buffer pointer        (handler's 4th argument)
 *   r6 = JSON-RPC request id                   (handler's 1st argument)
 *   [sp, #52] = the full 32-bit `params.index` (the stub pushes 10 registers at
 *               entry; `bl` itself does not push -- handler [sp,#12] + 40)
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
 *      (a write to TxModeReg 0x12 first runs the bring-up + select, so the
 *       reader is up, the tag is ACTIVE and the FIFO is empty before the host
 *       loads its frame)
 *   2  FIFO write byte              a1=index, a2=byte     -> 0
 *   3  PCD command                  a1=tx bytes, a2=cmd   -> ErrorReg
 *      (drains the host's pre-loaded FIFO frame -- up to 32 bytes -- into a
 *       stack buffer, re-asserts the slot and the antenna bits, runs the
 *       command with TxCRCEn|RxCRCEn and full-byte framing, and leaves the
 *       response in the FIFO for op 4/op 5)
 *   4  FIFO read byte               a1=index              -> FIFO byte
 *   5  received bits                a1=0                  -> count (bytes*8)
 *   6  reader enable + bring-up + REQA + anticollision + SELECT
 *      -> 0 = card present/ACTIVE, 2 = collision, 0xFF = no card/error.
 *      Leaves the 54-byte diagnostic record in the FIFO (op 0 register 0x0A
 *      gives the length, op 4 pops the bytes); "SCAN RECORD" below.
 *   7  acquire reader (explicit hold; reply keeps state 0) -> saved state byte
 *   8  release reader (a2 = saved state)                  -> 0
 *   Every reply path (including the marker and an unknown op) restores the
 *   recognition state captured at entry.
 *
 * Reader selection (fix round 6, re-derived from the firmware)
 * ------------------------------------------------------------
 * The recognition dispatcher 0x08015C94 -> state-11 handler 0x080159E4 selects
 * a channel at 0x08015A52/0x08015A5C with `0x0800E228(4, 0)` then
 * `0x0800E228(slot-1, 1)`; the state-6 reader path (0x08015B56) then brings the
 * same index up with 0x0800E314(reader, 1).  Line/mask map (see select_reader
 * and docs/notes-ops.md section 6): line 0..3 = PB10/PB11/PA14/PA13, `1` =
 * LOW (0x080095CC is BRR, 0x080095C8 is BSRR), and descriptor index N (tables
 * 0x20006240/0x200062A0) is the SPI channel whose CS is PB12 for {0,2} and PC6
 * for {1,3}.  reader = slot = 0..3.
 *
 * SCAN RECORD (left in the RC522 FIFO by op 6)
 * --------------------------------------------
 *   [0]   0x5A          magic
 *   [1]   reader        0..3
 *   [2]   code          0 = ACTIVE, 2 = collision, 0xFF = no card
 *   [3]   stages        1..5: how many stage records are valid
 *   stage k at 4 + 10k   (k = 0..4)
 *     +0,+1  frame        the frame prologue bytes (REQA 0x26 / 0x93 0x20 /
 *                         0x93 0x70 / 0x95 0x20 / 0x95 0x70; +0 is over-
 *                         written by the previous stage's nbytes spill)
 *     +2..+6 data         raw response bytes as received (FIFO order)
 *     +7     ComIrqReg    sampled after the stage's IRQ wait
 *     +8     ErrorReg     sampled after the stage's IRQ wait
 *     +9     FIFOLevelReg & 0x3F as sampled
 *   (the fourth meta byte, nbytes = min(level, 5), spills into the next
 *   stage's frame byte and is derived by the host as min(level, 5)).
 *   stages: 0 = REQA (0x26, BitFraming 0x07), 1 = anticollision CL1
 *           (0x93 0x20), 2 = SELECT CL1 (0x93 0x70 + UID + hardware CRC),
 *           3 = anticollision CL2 (0x95 0x20), 4 = SELECT CL2
 *           (0x95 0x70 + UID + hardware CRC).
 *   Total 4 + 5*10 = 54 bytes (fits FIFOLevelReg's 6-bit range).
 *
 * CFW primitives (all Thumb):
 *   read_reg(reader, reg)       -> byte  0x0800E734 (thumb 0x0800E735)
 *   write_reg(reader, reg, v)   -> 1     0x0800E7C0 (thumb 0x0800E7C1)
 *   set_bits(reader, reg, b)    -> 1     0x0800E8E0 (thumb 0x0800E8E1)
 *   reader_enable(idx, on)               0x0800E228 (thumb 0x0800E229)
 *   all_reader_init()                    0x0800F030 (thumb 0x0800F031)
 *   soft_reset(reader)                   0x0800E948 (thumb 0x0800E949)
 *   reader_config(reader, 0)             0x0800E9EC (thumb 0x0800E9ED)
 *   state_get() / state_set(v)           0x08011E98 / 0x08011EA4 (+1)
 *   osDelay(ms)                          0x0800BF1C (thumb 0x0800BF1D)
 *   JSON reply formatter                 0x0801DC74 (thumb 0x0801DC75)
 * The RC522 register set used (see docs/notes-ops.md):
 *   0x01 CommandReg, 0x02 ComIEnReg, 0x04 ComIrqReg, 0x06 ErrorReg,
 *   0x09 FIFODataReg, 0x0A FIFOLevelReg, 0x0C ControlReg, 0x0D BitFramingReg,
 *   0x12 TxModeReg, 0x13 RxModeReg, 0x14 TxControlReg, 0x2A TModeReg,
 *   0x37 VersionReg.
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
    .equ    ENABLE,      0x0800E229     /* reader_enable(idx, on) */
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
    .equ    R_TXCONTROL, 0x14
    .equ    R_TMODE,     0x2A
    .equ    PCD_TRANSCEIVE, 0x0C
    .equ    IRQ_MASK,    0x31           /* RxIRq|IdleIRq|TimerIRq */
    .equ    WAIT_ITERS,  0x0BB8         /* the firmware transceive's own bound */
    .equ    FLD_SETTLE,  100            /* the firmware antenna routine's delay */
    .equ    DRAIN_MS,    200            /* op7: let an in-flight action finish */
    .equ    SELECT_TRIES, 3             /* the stock state machine retries too */
    .equ    PCD_MAX_TX,  32             /* op3 frame buffer (stack) */

    .equ    REC_MAGIC,   0x5A
    .equ    REC_STAGES,  5
    .equ    REC_STRIDE,  10
    .equ    REC_SIZE,    54             /* 4 + 5*10 */
    .equ    REC_STAGE,   4              /* first stage record */
    .equ    REC_STACK,   56             /* select_card's record frame (8-aligned) */
    .equ    STG_FRAME,   0              /* stage record: frame prologue (2 B) */
    .equ    STG_DATA,    2              /* stage record: response bytes (5 B) */
    .equ    STG_META,    7              /* stage record: comirq/error/level */
    .equ    CMD_REQA,    0x26
    .equ    CMD_ANTICOLL,0x20
    .equ    CMD_SELECT,  0x70
    .equ    CMD_CL1,     0x93
    .equ    CMD_CL2,     0x95
    .equ    CRC_ON,      0x80           /* xcv_raw flags bit 7 */
    .equ    DRAIN_CAP,   5              /* bytes kept per stage */

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
     * TxModeReg (0x12) -- `_rc_setup_crc` in ace_rc522.py.  Bring the reader
     * up and the tag to ACTIVE on that first register write, BEFORE the host
     * writes its FIFO frame: the op-6 recipe (reset + config + SELECT) must
     * never run after the frame exists.  The select also flushes the FIFO, so
     * the host's frame starts clean and no diagnostic record can precede it.
     * Any other register write passes through untouched. */
    cmp     r7, #R_TXMODE
    bne     ow_go
    mov     r0, r6
    movs    r1, #0                  /* no diagnostic record (flushes only) */
    movs    r2, #SELECT_TRIES
    bl      select_card             /* code ignored: the write must land */
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

op_pcd:                             /* op3: PCD command -> ErrorReg */
    /* The host pre-loaded its bare frame (`30 <page>`) into the FIFO.  Pull
     * it into our own buffer, then run the command through xcv_raw (which
     * flushes, loads the frame from RAM and re-asserts the slot/antenna).
     * With resp = 0 the response stays in the FIFO, so op 5/op 4 read it
     * exactly as before.  a1 (whole TX bytes) stays informational. */
    sub     sp, #PCD_MAX_TX
    mov     r0, r6
    movs    r1, #R_FIFOLEVEL
    bl      rreg
    ands    r5, r0, #0x3F           /* TX bytes pre-loaded by op 2 */
    cmp     r5, #PCD_MAX_TX
    it      hi
    movhi   r5, #PCD_MAX_TX
    mov     r2, r5
    mov     r3, sp
    cbz     r2, op3_send
op3_drain:
    mov     r0, r6
    movs    r1, #R_FIFODATA
    bl      rreg
    strb    r0, [r3]
    adds    r3, #1
    subs    r2, r2, #1
    bne     op3_drain
op3_send:
    mov     r0, r6
    movs    r1, #0                  /* keep the reply frame in the FIFO */
    mov     r2, sp
    movs    r3, #CRC_ON             /* TxCRCEn|RxCRCEn, full-byte framing */
    mov     r12, r5
    bl      xcv_raw
    lsrs    r9, r0, #8              /* ErrorReg is the status byte */
    uxtb    r9, r9
    add     sp, #PCD_MAX_TX
    b       reply

op_select:                          /* op6: bring-up + REQA/anticoll/SELECT */
    mov     r0, r6
    movs    r1, #1                  /* leave the diagnostic record in the FIFO */
    movs    r2, #SELECT_TRIES
    bl      select_card
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

enable:                             /* r0=idx, r1=on */
    ldr     r12, =ENABLE
    bx      r12

reset:                              /* r0=reader; soft reset + 1 ms */
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
 * 0x08015A5C (`(4, 0)` then `(slot-1, 1)`), then the reader bring-up
 * 0x0800E314 (state 6: 0x08015B56 with the same index).
 *
 *   enable = 0x0800E228(idx, val), a jump table:
 *     idx 0..3: val==1 -> 0x080095CC (BRR, line LOW),
 *               val==0 -> 0x080095C8 (BSRR, line HIGH);
 *               line 0 = PB10, 1 = PB11, 2 = PA14, 3 = PA13;
 *     idx 4:    val is a 4-bit mask, bit i -> line i (1 = LOW).
 *   (constants at 0x0800E30C/0x0800E310; 0x0800E1C4 configures the pins)
 *
 * The SPI side is the descriptor index passed to read_reg/write_reg
 * (tables 0x20006240/0x200062A0, entry = index); entries {0,2} share
 * CS = PB12 and {1,3} share CS = PC6 (chip reset pulses PC9 / PA8 come from
 * 0x0800EECC/0x0800EE60 inside 0x0800F030 = init_all).  So index N selects
 * the same physical channel for both the line and the SPI chip: reader =
 * slot = 0..3, exactly the semantics the packed index exposes.
 *
 * r0 = reader.                                                                 */
select_reader:
    push    {r4, lr}
    mov     r4, r0
    movs    r0, #4
    movs    r1, #0
    bl      enable                  /* (4, 0): all selection lines HIGH */
    mov     r0, r4
    movs    r1, #1
    bl      enable                  /* (reader, 1): this channel LOW */
    pop     {r4, pc}

/* --- xcv_raw: our own RC522 transceive ------------------------------------ *
 * The RX path with the classic mistakes handled explicitly:
 *
 *   1. the FIFO is flushed, the frame is loaded from RAM (no dependence on a
 *      host pre-load), then the command is started with StartSend;
 *   2. the wait polls ComIrqReg for RxIRq|IdleIRq|TimerIRq (0x31) -- a
 *      response ends the wait, and the bound is the firmware's 0xBB8 so a dead
 *      field returns a status instead of hanging;
 *   3. ComIrqReg / ErrorReg / FIFOLevelReg are sampled BEFORE the FIFO is
 *      touched, and the IRQ flags are NOT cleared in between (clearing them
 *      first is the classic way to lose a received frame);
 *   4. the FIFO is drained only after the IRQ was seen, and any bytes present
 *      are returned even if TimerIRq/ErrIRq also fired: a tag only answers a
 *      frame it accepted, so data beats the timeout flag.
 *
 * r0  = reader
 * r1  = response buffer (NULL = leave the response in the FIFO for op 4/5)
 * r2  = frame pointer
 * r3  = flags: bits 0..2 = BitFramingReg last-byte bits (0 = full bytes,
 *       7 = the 7-bit REQA), bit 7 = TxCRCEn|RxCRCEn
 * r12 = frame length in bytes
 * returns r0 = ComIrqReg | ErrorReg<<8 | (FIFOLevelReg & 0x3F)<<16
 *               | min(level, 5)<<24                                             */
xcv_raw:
    push    {r4, r5, r6, r7, r8, lr}
    mov     r4, r0                  /* reader */
    mov     r5, r1                  /* response buffer */
    mov     r6, r2                  /* frame */
    mov     r7, r3                  /* flags */
    mov     r8, r12                 /* frame length */
    /* re-assert the slot and the transmitter (no reset: a pre-loaded frame
     * or an ACTIVE tag survives) */
    mov     r0, r4
    bl      select_reader
    mov     r0, r4
    movs    r1, #R_TXCONTROL
    movs    r2, #0x03
    bl      sbits
    /* flush the FIFO (FIFOLevelReg = 0x80) */
    mov     r0, r4
    movs    r1, #R_FIFOLEVEL
    movs    r2, #0x80
    bl      wreg
    /* CRC modes: bit 7 of TxModeReg/RxModeReg = flags bit 7 */
    ands    r2, r7, #CRC_ON
    mov     r0, r4
    movs    r1, #R_TXMODE
    bl      wreg
    ands    r2, r7, #CRC_ON
    mov     r0, r4
    movs    r1, #R_RXMODE
    bl      wreg
    /* BitFramingReg = last-byte bits (0 = full bytes, 7 = REQA) */
    ands    r2, r7, #7
    mov     r0, r4
    movs    r1, #R_BITFRAMING
    bl      wreg
    /* load the frame into the FIFO */
    cmp     r8, #0
    beq     xc_start
xc_push:
    ldrb    r2, [r6], #1
    mov     r0, r4
    movs    r1, #R_FIFODATA
    bl      wreg
    subs    r8, r8, #1
    bne     xc_push
xc_start:
    /* ComIrqReg = 0x7F (clear), ComIEnReg = 0xF7 (the firmware's own mask) */
    mov     r0, r4
    movs    r1, #R_COMIRQ
    movs    r2, #0x7F
    bl      wreg
    mov     r0, r4
    movs    r1, #R_COMIEN
    movs    r2, #0xF7
    bl      wreg
    /* CommandReg = 0 (idle) then 0x0C (TRANSCEIVE) */
    mov     r0, r4
    movs    r1, #R_COMMAND
    movs    r2, #0
    bl      wreg
    mov     r0, r4
    movs    r1, #R_COMMAND
    movs    r2, #PCD_TRANSCEIVE
    bl      wreg
    /* BitFramingReg = last-byte bits | StartSend */
    ands    r2, r7, #7
    orrs    r2, r2, #0x80
    mov     r0, r4
    movs    r1, #R_BITFRAMING
    bl      wreg
    /* bounded wait on ComIrqReg & 0x31 (never clears it) */
    movw    r8, #WAIT_ITERS
xc_wait:
    mov     r0, r4
    movs    r1, #R_COMIRQ
    bl      rreg
    ands    r0, r0, #IRQ_MASK
    bne     xc_done
    subs    r8, r8, #1
    bne     xc_wait
xc_done:
    /* clear StartSend (BitFraming = last-byte bits, full bytes when 0) */
    ands    r2, r7, #7
    mov     r0, r4
    movs    r1, #R_BITFRAMING
    bl      wreg
    /* sample the stage registers BEFORE touching the FIFO */
    mov     r0, r4
    movs    r1, #R_COMIRQ
    bl      rreg
    mov     r6, r0                  /* ComIrqReg */
    mov     r0, r4
    movs    r1, #R_ERROR
    bl      rreg
    mov     r7, r0                  /* ErrorReg (the flags register is free now) */
    mov     r0, r4
    movs    r1, #R_FIFOLEVEL
    bl      rreg
    ands    r0, r0, #0x3F
    mov     r8, r0                  /* FIFO level */
    /* nbytes = min(level, DRAIN_CAP) */
    movs    r3, #DRAIN_CAP
    cmp     r8, r3
    it      ls
    movls   r3, r8
    /* pack the return value */
    orr     r6, r6, r7, lsl #8
    orr     r6, r6, r8, lsl #16
    orr     r6, r6, r3, lsl #24
    /* drain the response (only after the IRQ was seen) */
    cbz     r5, xc_ret
    mov     r8, r3
    cmp     r8, #0
    beq     xc_ret
xc_drain:
    mov     r0, r4
    movs    r1, #R_FIFODATA
    bl      rreg
    strb    r0, [r5]
    adds    r5, #1
    subs    r8, r8, #1
    bne     xc_drain
xc_ret:
    mov     r0, r6
    pop     {r4, r5, r6, r7, r8, pc}

/* --- select_card: bring-up, retries, diagnostic record, FIFO dump --------- *
 * r0 = reader, r1 = want record (0/1; the op-1 trigger passes 0 so its select
 * flushes the FIFO and leaves nothing that could join the host's frame),
 * r2 = attempts -> r0 = 0 (ACTIVE) / 2 (collision) / 0xFF (no card).
 *
 * The record is built by seq_once() in this frame, then flushed FIFO + append
 * so the host reads it with op 0 (FIFOLevelReg 0x0A) + op 4.                    */
select_card:
    push    {r4, r5, r6, r7, r8, lr}
    sub     sp, #REC_STACK
    mov     r4, r0                  /* reader */
    mov     r5, sp                  /* record */
    mov     r6, r2                  /* attempts left */
    mov     r8, r1                  /* want record */
    bl      init_all                /* pin/table setup + reset pulses, once */
    mov     r0, r4
    bl      select_reader
sc_attempt:
    mov     r0, r4
    mov     r1, r5
    bl      seq_once                /* r0 = code, r1 = stages */
    mov     r7, r1
    cmp     r0, #0
    beq     sc_done
    subs    r6, r6, #1
    bne     sc_attempt
sc_done:
    mov     r6, r0                  /* keep the code across the dump */
    cmp     r8, #0
    beq     sc_flush
    movs    r0, #REC_MAGIC
    strb    r0, [r5]
    strb    r4, [r5, #1]
    strb    r6, [r5, #2]
    strb    r7, [r5, #3]
sc_flush:
    mov     r0, r4
    movs    r1, #R_FIFOLEVEL
    movs    r2, #0x80
    bl      wreg
    cmp     r8, #0
    beq     sc_ret
    mov     r8, r5                  /* record cursor */
    movs    r7, #REC_SIZE           /* byte count */
sc_dump:
    ldrb    r2, [r8], #1
    mov     r0, r4
    movs    r1, #R_FIFODATA
    bl      wreg
    subs    r7, r7, #1
    bne     sc_dump
sc_ret:
    mov     r0, r6
    add     sp, #REC_STACK
    pop     {r4, r5, r6, r7, r8, pc}

/* --- seq_once: one bring-up + one REQA/anticollision/SELECT sequence ------ *
 * r0 = reader, r1 = record -> r0 = code (0/2/255), r1 = stages attempted.
 *
 * Bring-up order mirrors the firmware's own mode-1 entry 0x0800E314(reader, 1):
 * soft reset -> antenna bits -> 100 ms -> component config -> 3 ms.  Then:
 *
 *   stage 0  REQA        0x26, BitFraming 0x07, no CRC     -> ATQA
 *   stage 1  anticoll CL1 0x93 0x20, full bytes, no CRC    -> 5 UID bytes
 *   stage 2  SELECT CL1  0x93 0x70 + UID + hardware CRC    -> SAK (1 B)
 *   stage 3  anticoll CL2 0x95 0x20 (only if SAK bit 2)    -> 5 UID bytes
 *   stage 4  SELECT CL2  0x95 0x70 + UID + hardware CRC    -> SAK (1 B)
 *
 * Each stage's frame prologue reuses the previous record's spill byte, and the
 * anticollision response is captured straight into the stage's data slot, so
 * the SELECT frame is already assembled: [cmd][0x70][UID0..4].  The 7 -> 0
 * BitFraming transition is explicit: every xcv_raw call programs BitFramingReg
 * from its own flags.  A stage is accepted when the response bytes are present,
 * whatever the error flags say; the flags stay in the record.                 */
seq_once:
    push    {r4, r5, r6, lr}
    mov     r4, r0
    mov     r5, r1
    /* --- bring-up --- */
    mov     r0, r4
    bl      reset                   /* CommandReg = 0x0F + 1 ms */
    movs    r0, #2
    bl      delay
    mov     r0, r4
    movs    r1, #R_TXCONTROL
    movs    r2, #0x03
    bl      sbits                   /* TxControlReg |= 3, unconditionally */
    movs    r0, #FLD_SETTLE
    bl      delay                   /* the firmware's own 100 ms field settle */
    mov     r0, r4
    movs    r1, #0
    bl      config                  /* 0x0800E9EC(reader, 0) */
    movs    r0, #3
    bl      delay
    mov     r0, r4
    bl      select_reader
    /* --- stage 0: REQA (7-bit frame, no CRC) --- */
    movs    r6, #1
    movs    r0, #CMD_REQA
    strb    r0, [r5, #REC_STAGE + STG_FRAME]
    mov     r0, r4
    add     r1, r5, #REC_STAGE + STG_DATA
    add     r2, r5, #REC_STAGE + STG_FRAME
    movs    r3, #0x07               /* 7-bit last byte */
    movs    r12, #1
    bl      xcv_raw
    str     r0, [r5, #REC_STAGE + STG_META]
    lsrs    r1, r0, #24
    cmp     r1, #2                  /* ATQA = 2 bytes */
    blo     sq_fail
    /* --- stage 1: anticollision CL1 --- */
    movs    r6, #2
    movs    r0, #CMD_CL1
    strb    r0, [r5, #14]
    movs    r0, #CMD_ANTICOLL
    strb    r0, [r5, #15]
    mov     r0, r4
    add     r1, r5, #16
    add     r2, r5, #14
    movs    r3, #0                  /* full bytes, no CRC */
    movs    r12, #2
    bl      xcv_raw
    str     r0, [r5, #21]
    lsrs    r1, r0, #24
    cmp     r1, #5                  /* 4 UID bytes + BCC */
    blo     sq_fail
    /* --- stage 2: SELECT CL1 (reuses the assembled [0x93][0x70][UID]) --- */
    movs    r6, #3
    movs    r0, #CMD_CL1
    strb    r0, [r5, #14]
    movs    r0, #CMD_SELECT
    strb    r0, [r5, #15]
    mov     r0, r4
    add     r1, r5, #26
    add     r2, r5, #14
    movs    r3, #CRC_ON             /* TxCRCEn|RxCRCEn, full bytes */
    movs    r12, #7
    bl      xcv_raw
    str     r0, [r5, #31]
    lsrs    r1, r0, #24
    cbz     r1, sq_fail             /* no SAK at all */
    ldrb    r1, [r5, #26]
    lsls    r1, r1, #29             /* SAK bit 2 = still cascading */
    bpl     sq_ok
    /* --- stage 3: anticollision CL2 --- */
    movs    r6, #4
    movs    r0, #CMD_CL2
    strb    r0, [r5, #34]
    movs    r0, #CMD_ANTICOLL
    strb    r0, [r5, #35]
    mov     r0, r4
    add     r1, r5, #36
    add     r2, r5, #34
    movs    r3, #0
    movs    r12, #2
    bl      xcv_raw
    str     r0, [r5, #41]
    lsrs    r1, r0, #24
    cmp     r1, #5
    blo     sq_fail
    /* --- stage 4: SELECT CL2 --- */
    movs    r6, #5
    movs    r0, #CMD_CL2
    strb    r0, [r5, #34]
    movs    r0, #CMD_SELECT
    strb    r0, [r5, #35]
    mov     r0, r4
    add     r1, r5, #46
    add     r2, r5, #34
    movs    r3, #CRC_ON
    movs    r12, #7
    bl      xcv_raw
    str     r0, [r5, #51]
    lsrs    r1, r0, #24
    cbz     r1, sq_fail
    ldrb    r1, [r5, #46]
    lsls    r1, r1, #29             /* cascade bit clear -> ACTIVE */
    bmi     sq_fail
sq_ok:
    movs    r0, #0
sq_ret:
    mov     r1, r6
    pop     {r4, r5, r6, pc}
sq_fail:
    /* r0 = the failing stage's packed value: CollErr (0x10) -> 2, else 255 */
    lsrs    r1, r0, #8
    ands    r1, r1, #0x10
    movs    r0, #0xFF
    it      ne
    movne   r0, #2
    b       sq_ret

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

    .align  2
value_fmt:
    .asciz  "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"ok\"}"
