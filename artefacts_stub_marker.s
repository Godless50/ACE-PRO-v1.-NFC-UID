/* artefacts_stub_marker.s — P2 reachability marker + value probe (Gen 1 RC522).
 *
 * Copyright (C) 2026 ACE-UID contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Purpose
 * -------
 * The P2 image booted but `result.code` still never arrived, and the device
 * evidence cannot tell apart:
 *   (A) the hook at VA 0x0801450A is not executed on this path (so this stub
 *       never runs and the stock handler answers by itself), or
 *   (B) the stub runs but its reply/decision is wrong.
 *
 * This diagnostic stub is called from the same hook (VA 0x0801450A, inside the
 * `filament_recognition` handler 0x080144D4; the displaced instruction is
 * `bl 0x0801DC74`) and answers by INDEX VALUE, without needing the register
 * read:
 *
 *   index 0..3          -> replay the displaced `bl 0x0801DC74` unchanged
 *                          (ordinary slots behave exactly as stock)
 *   index >= 4          -> MARKER: {"id":%d,"result":{"code":90},"msg":"marker"}
 *                          (90 = 0x5A; fires for ANY non-slot index, so it does
 *                          not depend on the packed encoding or on read_reg)
 *   bit 31 set          -> VALUE: read a register and answer
 *                          {"id":%d,"result":{"code":%d},"msg":"ok"}
 *                          (op 0, reader/a1 decoded from the payload)
 *
 * Interface at the hook (see docs/notes-p2-reply.md)
 * --------------------------------------------------
 *   r0 = response/output buffer pointer        (handler's 4th argument)
 *   r1 = response JSON format string           (0x08021130)
 *   r2 = JSON-RPC request id                   (handler's 1st argument)
 *   r3 = response message string               (0x08020C7C / 0x080210FC)
 *   [sp, #12] = the full 32-bit decoded `params.index` (little-endian word)
 *
 *   WARNING — this stub still reads `[sp, #16]` below: that is the P2b bug and
 *   is kept only because this historical diagnostic image's md5 is pinned.
 *   On Cortex-M `bl` does not push, so the live slot is `[sp,#12]`; `[sp,#16]`
 *   is the adjacent uninitialised local (a stale value >= 4 with bit 31 clear,
 *   which is why this image's marker fired for every index).  See
 *   docs/notes-p2-reply.md §9.  Do NOT copy this read: the delivered stub
 *   `artefacts_stub_probe.s` uses `[sp,#12]`.
 *   lr = 0x0801450F (return into the handler epilogue)
 *
 * The handler parses `params.index` with the signed `strtol` core
 * (0x0801863C -> 0x0801C2C6 -> _strtol_r 0x0801CEE0), so a host value >= 2^31
 * arrives clamped to LONG_MAX = 0x7FFFFFFF (payload lost).  The marker fires
 * for any value >= 4, which covers both the clamped and the signed forms.
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
    ldr     r12, [sp, #16]          /* P2b BUG: should be [sp,#12] (notes §9) */
    lsrs    r5, r12, #31
    bne     value                   /* bit 31 -> packed value probe */

    cmp     r12, #4
    bhs     marker                  /* any non-slot index -> reachability marker */

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
    ldr     r2, [sp, #4]            /* id */
    ldr     r0, [sp, #0]            /* response buffer */
    adr     r1, marker_fmt
    ldr     r12, =RESPOND
    blx     r12
    pop     {r0, r2, lr}
    bx      lr

    /* --- value probe (bit 31 payload): read register, answer in code ------ */
value:
    push    {r0, r2, r4, r5, lr}
    ubfx    r4, r12, #24, #2        /* reader */
    ubfx    r5, r12, #8,  #6        /* a1 = register */
    mov     r0, r4
    mov     r1, r5
    ldr     r12, =READ_REG
    blx     r12                     /* r0 = register value */
    mov     r3, r0
    ldr     r2, [sp, #4]
    ldr     r0, [sp, #0]
    adr     r1, value_fmt
    ldr     r12, =RESPOND
    blx     r12
    pop     {r0, r2, r4, r5, lr}
    bx      lr

    .align  2
marker_fmt:
    .asciz  "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"marker\"}"
value_fmt:
    .asciz  "{\"id\":%d,\"result\":{\"code\":%d},\"msg\":\"ok\"}"

    .equ    READ_REG, 0x0800E735
    .equ    RESPOND,  0x0801DC75
