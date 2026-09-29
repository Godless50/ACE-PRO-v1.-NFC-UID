/* artefacts_stub_uid_cfw.s — canonical source of the frozen "parser UID" stub
 * already present in the production image ACE_V1.3.863_cfw_uid.bin
 * (md5 6148cfc52431fc235536c6d64b5334ef, 113828 B).
 *
 * Copyright (C) 2026 ACE-UID contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Provenance: this is the 108-byte blob appended at VA 0x08023C38 in the
 * recorded production image (see REPORT-EN.md, Appendix C).  It was recovered
 * by disassembling the recorded tail and re-expressing it as Thumb-2 source;
 * `arm-none-eabi-as`/`-ld -Ttext 0x08023C38`/`-objcopy` reproduces the blob
 * byte-for-byte (see tests/test_build_rc522_tunnel.py).
 *
 * It is the *existing* parser patch and, per the plan's Global Constraints, its
 * bytes must never change: the builder treats it as frozen.
 *
 * Behaviour: hooks the fallback branch of the stock tag parser.  r3 points at
 * the parser context.  If the 4-byte UID buffer at 0x20006224 is non-zero, it
 * writes the 8-byte UID as uppercase hex + NUL at ctx+60, sets a type byte and
 * the "{e" marker, and lets the caller continue; otherwise it replays the
 * displaced instruction `ldrb.w r1, [r3, #0x36]` and returns.
 *
 * NOTE: artefacts_stub4.s in this repository is a *different* stub (the
 * stock-image in-place route, assembles to 120 bytes with a lowercase hex
 * table).  It is not the source of this tail; this file is.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb
    .text
    .global _start
_start:
    push    {r4, r5, r6, lr}
    ldr     r5, lit_uidbuf
    ldr     r4, [r5, #0]
    ldr.w   r6, [r5, #3]
    orrs    r4, r6
    beq     done
    adr     r6, hexlut
    add.w   r4, r3, #60
    mov.w   ip, #0
loop:
    ldrb.w  r1, [r5, ip]
    mov.w   lr, r1, lsr #4
    ldrb.w  lr, [r6, lr]
    strb.w  lr, [r4], #1
    and.w   r1, r1, #15
    ldrb    r1, [r6, r1]
    strb.w  r1, [r4], #1
    add.w   ip, ip, #1
    cmp.w   ip, #7
    blt     loop
    movs    r1, #0
    strb    r1, [r4, #0]
    movs    r1, #2
    strb.w  r1, [r3, #54]
    movs    r1, #123
    strh    r1, [r3, #56]
    movs    r1, #101
    strh    r1, [r3, #58]
    movs    r1, #1
    pop     {r4, r5, r6, pc}
done:
    ldrb.w  r1, [r3, #54]
    pop     {r4, r5, r6, pc}
    .align  2
hexlut:
    .ascii  "0123456789ABCDEF"
    .align  2
lit_uidbuf:
    .word   0x20006224
