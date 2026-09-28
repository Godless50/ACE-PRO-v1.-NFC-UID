/* stub_tunnel5.s — RFID-туннель v5 (ACE V1.3.863 gen 1), Thumb-2
 * Врезка: 0x0800ED58 (вместо bl 0x08013458) — ДО проверки состояния,
 * иначе при *(0x200001B9)!=2 обработчик печатает FORBIDDEN и хук пропускает.
 * На входе [sp,#4] = полное 32-битное params.index (наш push{lr} => [sp,#8]).
 * Туннель: бит 17 = магия; биты 16-15 = считыватель; бит 14 = доп.флаг;
 * биты 13-8 = регистр (0x3D статус, 0x3E сырой UID); биты 7-0 = данные/смещение.
 *   0x3E,бит14=1 — опознать метку и вернуть 4 байта буфера по смещению data
 *   0x3E,бит14=0 — просто вернуть 4 байта буфера по смещению data
 *   0x3C        — вернуть выбранный прошивкой слот *(0x200001C0)
 *   0x3D        — вернуть статус 0x20000127
 *   0x3F        — вернуть статус 0x20000127
 * Ответ: {"id":%d,"v":%d} через штатный printf 0x080159CC.
 */
    .syntax unified
    .cpu cortex-m4
    .thumb
    .text
    .global _start
_start:
    push    {lr}
    ldr     r3, [sp, #8]
    lsrs    r0, r3, #17
    cbnz    r0, tunnel
    ldr     r12, =0x08013459
    blx     r12
    pop     {pc}
tunnel:
    ubfx    r0, r3, #15, #2
    ubfx    r1, r3, #14, #1
    ubfx    r2, r3, #8,  #6
    uxtb    r3, r3
    cmp     r2, #0x3C
    beq     t_slot
    cmp     r2, #0x3E
    beq     t_uid
    cmp     r2, #0x3F
    beq     t_stat
    cmp     r2, #0x3D
    beq     t_stat
    cbz     r1, tread
    mov     r1, r2
    mov     r2, r3
    ldr     r12, =0x08019E1D
    blx     r12
    mov     r3, r0
    b       reply
tread:
    mov     r1, r2
    ldr     r12, =0x08019D13
    blx     r12
    mov     r3, r0
    b       reply
t_slot:
    ldr     r2, =0x200001C0
    ldrb    r3, [r2, #0]
    b       reply
t_stat:
    ldr     r2, =0x20000127
    ldrb    r3, [r2, #0]
    b       reply
t_uid:
    push    {r0, r3, r4, r7}
    cbz     r1, t_pack
    ldr     r12, =0x08013255
    blx     r12
    ldr     r0, [sp, #0]
    movs    r1, #1
    ldr     r12, =0x0800A6A1
    blx     r12
t_pack:
    pop     {r0, r1, r4, r7}
    bl      pack4
    b       reply
pack4:
    ldr     r2, =0x200067D0
    adds    r2, r2, r1
    ldrb    r0, [r2, #0]
    ldrb    r1, [r2, #1]
    lsls    r0, r0, #24
    lsls    r1, r1, #16
    orrs    r0, r0, r1
    ldrb    r1, [r2, #2]
    lsls    r1, r1, #8
    orrs    r0, r0, r1
    ldrb    r1, [r2, #3]
    orrs    r0, r0, r1
    mov     r3, r0
    bx      lr
reply:
    mov     r2, r4
    adr     r1, fmt
    mov     r0, r7
    ldr     r12, =0x080159CD
    blx     r12
    add     sp, sp, #4
    ldr     r12, =0x0800ED7F
    blx     r12
    .align 2
fmt:
    .asciz "{\"id\":%d,\"v\":%d}"
