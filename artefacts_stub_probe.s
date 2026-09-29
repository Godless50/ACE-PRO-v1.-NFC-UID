/* artefacts_stub_probe.s — P1/P2/P3 probe stub for the ACE Pro (Gen 1) RC522 tunnel.
 *
 * Copyright (C) 2026 ACE-UID contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Hook site
 * ---------
 * Called from inside the `filament_recognition` handler at VA 0x0801450A
 * (function 0x080144D4; ACE command-table pair 0x08014674 -> name
 * "filament_recognition" at 0x08021508).  The hook replaces the handler's
 * response call `bl 0x0801DC74` (`f009 fbb3`, file `09f0b3fb`) with a `bl` here.
 *
 * Where the parsed index lives  (P3 fix — the P1/P2 bug)
 * ------------------------------------------------------
 * The hook is a `bl`, and on ARM Cortex-M `bl` does **not** push to the stack
 * (it only writes LR).  The callee therefore starts with the CALLER's SP, so at
 * stub entry SP == the handler's SP and the parsed `params.index` is at the
 * handler's own `[sp,#12]`, i.e. the stub's `[sp,#12]`.
 *
 *   handler prologue: push {r4,r5,r6,r7,lr} (20 B) ; sub sp,#20  -> local base
 *   parse:            str r5,[sp,#12]           (0x080144EC zeroes it)
 *                     0x0801863C -> 0x0801C2C6 -> _strtol_r 0x0801CEE0
 *                     ... -> str r0,[r1,#0] at 0x08018672, r1 = handler [sp,#12]
 *   success path:     ldrb.w r7,[sp,#12]        (0x08014512 reads its low byte)
 *
 * The earlier revision read `[sp,#16]`, which is the adjacent uninitialised
 * local of the handler frame — a stale value >= 4 with bit 31 clear, so every
 * index (even 2) wrongly took the ">= 4" branch.  Fixed here.
 *
 * Interface at the hook
 * ---------------------
 *   r0 = response/output buffer pointer        (handler's 4th argument)
 *   r1 = response JSON format string           (0x08021130)
 *   r2 = JSON-RPC request id                   (handler's 1st argument)
 *   r3 = response message string               (0x08020C7C / 0x080210FC)
 *   [sp, #12] = the full 32-bit decoded `params.index` (little-endian word)
 *   lr = 0x0801450F (return into the handler epilogue)
 *
 * Behaviour (host contract for the Gen 1 signed parser)
 * -----------------------------------------------------
 *   1. bit 31 set  -> CV1.3.863-style PACKED REQUEST (the CORRECT host form,
 *      e.g. INDEX=-2147469568 = 0x80003700).  Decode reader=(i>>24)&3 and
 *      a1=(i>>8)&0x3F, call read_reg(reader,a1) and reply
 *      {"id":%d,"result":{"code":%d},"msg":"ok"} -> result.code = value
 *      (op 0; reader 0 / a1 0x37 gives 161 = 0xA1).
 *   2. index == 0x7FFFFFFF -> the signed-`strtol` clamp sentinel produced by the
 *      UNSIGNED host form (INDEX=2147497728).  The payload is lost; this is a
 *      LOSSY FALLBACK and is mapped to the fixed reader 0 / op 0 / a1 0x37 probe
 *      so the probe still answers.  Use the signed form for the real tunnel.
 *   3. index >= 0x10000 (bit 31 clear) -> MARKER: constant code 90, msg
 *      "marker", no hardware access.  Off the normal path (real indices are
 *      0..3 or packed); it exists so a later probe can prove reachability.
 *   4. index 0..3 (and anything else) -> replay the displaced `bl 0x0801DC74`
 *      with r0..r3 untouched: byte-for-byte stock behaviour.
 *
 * CFW primitives: read_reg(reader, reg) -> byte 0x0800E734 (thumb 0x0800E735);
 * JSON reply formatter 0x0801DC74 (thumb 0x0801DC75), the function the hook
 * displaced.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb
    .text
    .global _start
_start:
    ldr     r12, [sp, #12]          /* params.index: handler [sp,#12] */
    lsrs    r5, r12, #31
    bne     packed                  /* bit 31 -> correct packed form */

    ldr     r5, =SENTINEL           /* 0x7FFFFFFF = unsigned form, clamped */
    cmp     r12, r5
    beq     fixed

    ldr     r5, =MARKER_MIN         /* 0x10000 */
    cmp     r12, r5
    bhs     marker                  /* large non-packed index -> marker */

    /* --- ordinary slot index: replay the displaced response call ---------- */
normal:
    push    {lr}
    ldr     r12, =RESPOND
    blx     r12
    pop     {pc}

    /* --- reachability marker: constant 90, no hardware access ------------- */
marker:
    push    {r0, r2, lr}
    movs    r3, #90                 /* 0x5A */
    ldr     r2, [sp, #4]
    ldr     r0, [sp, #0]
    adr     r1, marker_fmt
    ldr     r12, =RESPOND
    blx     r12
    pop     {r0, r2, lr}
    bx      lr

    /* --- clamped sentinel: fixed P2 probe (payload lost) ------------------ */
fixed:
    movs    r4, #0                  /* reader 0 */
    movs    r5, #0x37               /* VersionReg */
    b       read

    /* --- packed request: decode reader/a1 from the payload ---------------- */
packed:
    ubfx    r4, r12, #24, #2        /* reader */
    ubfx    r5, r12, #8,  #6        /* a1 = register */
read:
    push    {r0, r2, lr}
    mov     r0, r4
    mov     r1, r5
    ldr     r12, =READ_REG
    blx     r12                     /* r0 = register value (expect 0xA1) */
    mov     r3, r0                  /* value -> second %d */
    ldr     r2, [sp, #4]            /* id -> first %d */
    ldr     r0, [sp, #0]            /* response buffer */
    adr     r1, value_fmt
    ldr     r12, =RESPOND
    blx     r12
    pop     {r0, r2, lr}
    bx      lr

    .align  2
marker_fmt:
    .asciz  "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"marker\"}"
value_fmt:
    .asciz  "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"ok\"}"

    .equ    SENTINEL,   0x7FFFFFFF
    .equ    MARKER_MIN, 0x00010000
    .equ    READ_REG,   0x0800E735
    .equ    RESPOND,    0x0801DC75
