    .syntax unified
    .thumb
    .text
    .global _start
_start:
    stmdb   sp!, {r0-r11, lr}
    sub     sp, #4
    ldr     r0, lit_status
    ldrb    r0, [r0]
    strb    r0, [r4, #0x8A]
    ldrb    r0, [r4, #4]
    cbz     r0, chk
    b       done
chk:
    ldr     r0, lit_status
    ldrb    r0, [r0]
    cmp     r0, #1
    bne     done
    add     r0, r4, #4
    ldr     r1, lit_uid
    ldr     r12, lit_lut
    movs    r2, #7
loop:
    ldrb    r3, [r1]
    adds    r1, #1
    lsrs    r4, r3, #4
    ldrb    r4, [r12, r4]
    strb    r4, [r0]
    adds    r0, #1
    lsls    r3, r3, #28
    lsrs    r3, r3, #28
    ldrb    r3, [r12, r3]
    strb    r3, [r0]
    adds    r0, #1
    subs    r2, #1
    bne     loop
    movs    r3, #0
    strb    r3, [r0]
done:
    add     sp, #4
    ldmia.w sp!, {r0-r11}
    add     sp, #4
    bl      orig_snprintf
    b.w     ret_addr
    .align  2
lit_status: .word 0x20000127
lit_uid:    .word 0x2000012B
lit_lut:    .word hexlut
hexlut:
    .byte 0x30,0x31,0x32,0x33,0x34,0x35,0x36,0x37
    .byte 0x38,0x39,0x61,0x62,0x63,0x64,0x65,0x66
