#!/usr/bin/env python3
"""build_tunnel2.py — ACE V1.3.863 + RFID-туннель v2 (без гейта: пещер всего 258 Б).
Одна врезка: 0x0800ED58 (BL -> cave, stub_tunnel5.s)."""
import hashlib, struct, subprocess, sys
BASE=0x08008000
SRC='./ACE_V1.3.863_stock.bin'
SRC_MD5='dcd04589dcadd5b4feab66d33e772531'
OUT='./ACE_V1.3.863_tunnel5.bin'
TMP='.'
CAVE_LO,CAVE_HI=0x08020000,0x08021C00
md5=lambda b: hashlib.md5(b).hexdigest()
def crc16(data):
    crc=0xFFFF
    for b in data:
        crc^=b
        for _ in range(8): crc=(crc>>1)^0x8408 if crc&1 else crc>>1
    return crc&0xFFFF
def find_cave(img,size):
    run,start=0,None
    for va in range(CAVE_LO,CAVE_HI,4):
        if img[va-BASE:va-BASE+4]==b'\0\0\0\0':
            if run==0: start=va
            run+=4
            if run>=size: return start
        else: run=0
    return None
def branch(src,dst,link):
    o=dst-(src+4)
    s=(o>>24)&1; i1=(o>>23)&1; i2=(o>>22)&1
    imm10=(o>>12)&0x3FF; imm11=(o>>1)&0x7FF
    j1=(~(i1^s))&1; j2=(~(i2^s))&1
    hw1=0xF000|(s<<10)|imm10
    hw2=(0xD000 if link else 0x9000)|(j1<<13)|(j2<<11)|imm11
    return struct.pack('<HH',hw1,hw2)
def asm_link(src_s,cave_va):
    o,elf,obj=f'{TMP}/t2.o',f'{TMP}/t2.elf',f'{TMP}/t2.bin'
    subprocess.run(['arm-none-eabi-as','-mcpu=cortex-m4','-mthumb','-o',o,src_s],check=True)
    subprocess.run(['arm-none-eabi-ld','-Ttext',hex(cave_va),'-o',elf,o],check=True)
    subprocess.run(['arm-none-eabi-objcopy','-O','binary',elf,obj],check=True)
    return open(obj,'rb').read()
def main():
    stock=open(SRC,'rb').read(); img=bytearray(stock)
    print('src md5=%s len=%d'%(md5(stock),len(stock)))
    assert md5(stock)==SRC_MD5
    HB=0x0800ED58; OB=bytes.fromhex('04f07efb')
    assert bytes(img[HB-BASE:HB-BASE+4])==OB,'hook B bytes'
    tb=asm_link(f'{TMP}/stub_tunnel5.s',CAVE_LO)
    cave=find_cave(img,len(tb)); assert cave,'нет пещеры'
    tb=asm_link(f'{TMP}/stub_tunnel5.s',cave)
    assert find_cave(img,len(tb))==cave
    img[cave-BASE:cave-BASE+len(tb)]=tb
    img[HB-BASE:HB-BASE+4]=branch(HB,cave,link=1)
    print('tunnel stub %dB -> cave %s, hook %s'%(len(tb),hex(cave),img[HB-BASE:HB-BASE+4].hex()))
    data=bytes(img); open(OUT,'wb').write(data)
    diff=[i for i in range(len(data)) if data[i]!=stock[i]]
    rng=[]
    for i in diff:
        va=i+BASE
        if rng and va==rng[-1][1]+1: rng[-1][1]=va
        else: rng.append([va,va])
    print('OUT %s len=%d md5=%s sha256=%s crc16=%s'%(OUT,len(data),md5(data),hashlib.sha256(data).hexdigest(),hex(crc16(data))))
    print('diff %d B in %d ranges'%(len(diff),len(rng)))
    for lo,hi in rng: print('  %s..%s (%d B)'%(hex(lo),hex(hi),hi-lo+1))
if __name__=='__main__': sys.exit(main())
