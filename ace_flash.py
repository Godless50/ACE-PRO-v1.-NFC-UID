#!/usr/bin/env python3
"""ACE Pro Gen1 IAP flasher + probe (from RE of V1.3.863).

Wire frame:  FF AA | len(u16 LE) | payload | crc16(payload) | FE
  payload[0] == 0x55  -> IAP data chunk: 55 | addr(u32 LE) | n(u8) | data[n]
  otherwise           -> JSON-RPC request
Staging flash base 0x08024000; max chunk 64 bytes.
CRC-16/MCRF4XX (poly 0x8408, init 0xFFFF).
The ACE resets itself when idle for ~3.3 s, so the link must be kept busy.
"""
import json, struct, sys, time


def crc16(buf):
    crc = 0xFFFF
    for b in buf:
        d = b
        d ^= crc & 0xFF
        d ^= (d & 0x0F) << 4
        crc = ((d << 8) | (crc >> 8)) ^ (d >> 4) ^ (d << 3)
    return crc & 0xFFFF


def frame(p):
    return b"\xff\xaa" + struct.pack("<H", len(p)) + p + struct.pack("<H", crc16(p)) + b"\xfe"


class Ace:
    def __init__(self, dev, baud=115200, tries=20):
        self.dev = dev
        self.baud = baud
        self.tries = tries
        self.buf = b""
        self.id = 0
        self.ser = None
        self._open()

    def _open(self):
        import serial
        last = None
        for _ in range(self.tries):
            try:
                self.ser = serial.Serial(self.dev, self.baud, timeout=0.3,
                                         rtscts=True, exclusive=True)
                return
            except Exception as e:
                last = e
                time.sleep(1)
        raise SystemExit("cannot open %s: %s" % (self.dev, last))

    def reopen(self):
        self.close()
        self.buf = b""
        self._open()

    def prime(self, tries=40):
        for _ in range(tries):
            try:
                if self.rpc("get_status", timeout=1.0) is not None:
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def _extract(self):
        out = []
        while True:
            i = self.buf.find(b"\xff\xaa")
            if i < 0:
                self.buf = b""
                break
            self.buf = self.buf[i:]
            if len(self.buf) < 7:
                break
            ln = struct.unpack("<H", self.buf[2:4])[0]
            total = 4 + ln + 3
            if len(self.buf) < total:
                break
            out.append(self.buf[4:4 + ln])
            self.buf = self.buf[total:]
        return out

    def pump(self, dur):
        t = time.time()
        out = []
        while time.time() - t < dur:
            d = self.ser.read(4096)
            if d:
                self.buf += d
            out += self._extract()
        return out

    def rpc(self, method, params=None, timeout=4.0):
        self.id += 1
        rid = self.id
        req = {"id": rid, "method": method}
        if params is not None:
            req["params"] = params
        p = json.dumps(req, separators=(",", ":")).encode()
        self.ser.reset_input_buffer()
        self.buf = b""
        self.ser.write(frame(p))
        self.ser.flush()
        t = time.time()
        while time.time() - t < timeout:
            for f in self.pump(0.2):
                try:
                    j = json.loads(f)
                except Exception:
                    continue
                if j.get("id") == rid:
                    return j
        return None

    def chunk(self, addr, data):
        p = bytes([0x55]) + struct.pack("<I", addr) + bytes([len(data)]) + data
        self.ser.write(frame(p))
        self.ser.flush()

    def keepalive(self, dur, ivl=0.2):
        t = time.time()
        while time.time() - t < dur:
            try:
                self.rpc("get_status", timeout=1.0)
            except Exception:
                try:
                    self.reopen()
                except Exception:
                    pass
            time.sleep(ivl)

    def close(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass


def mode_version(a):
    print("get_info   :", a.rpc("get_info"))
    print("iap_version:", a.rpc("iap_version"))
    print("get_status :", a.rpc("get_status"))


def mode_ping(a, dur=20.0, ivl=0.2):
    t0 = time.time()
    n = ok = 0
    while time.time() - t0 < dur:
        try:
            r = a.rpc("get_status", timeout=1.5)
        except Exception as e:
            print("t=%.1f EXC %s" % (time.time() - t0, e), flush=True)
            time.sleep(0.5)
            continue
        n += 1
        if r is not None:
            ok += 1
        print("t=%5.1f ok=%s" % (time.time() - t0, r is not None), flush=True)
        time.sleep(ivl)
    print("sent %d, ok %d" % (n, ok))


def mode_finfo(a):
    for idx in range(4):
        print("f%d:" % idx, a.rpc("get_filament_info", {"index": idx}, 4.0), flush=True)


def mode_probe(a, idx, cycles=30):
    def try_rpc(method, params=None, tout=2.0):
        try:
            return a.rpc(method, params, timeout=tout)
        except Exception as e:
            print("  exc %s" % e, flush=True)
            try:
                a.reopen()
            except Exception as e2:
                print("  reopen failed %s" % e2, flush=True)
            return None

    print("reco:", try_rpc("filament_recognition", {"index": idx}, 4.0), flush=True)
    for i in range(cycles):
        s = try_rpc("get_status")
        if s is not None:
            slots = s.get("result", {}).get("slots", [])
            st = next((x for x in slots if x.get("index") == idx), None)
            print("  poll %d: %s" % (i, st), flush=True)
        time.sleep(0.5)
    print("info:", try_rpc("get_filament_info", {"index": idx}, 4.0), flush=True)


def mode_rate(a, idx, n=10, wait=5.0):
    ok = 0
    vals = []
    for i in range(n):
        a.rpc("filament_recognition", {"index": idx}, timeout=3.0)
        a.keepalive(wait)
        r = a.rpc("get_filament_info", {"index": idx}, timeout=3.0)
        sku = (r or {}).get("result", {}).get("sku", None) if r else None
        print("  %2d sku=%r" % (i, sku), flush=True)
        if sku:
            ok += 1
            vals.append(sku)
    print("SLOT %d RESULT: %d/%d reads, values=%s" % (idx, ok, n, sorted(set(vals))),
          flush=True)


def mode_hunt(a, idx, max_mm=60, step=20, speed=40, wait=1.4):
    moved = 0
    found = None
    try:
        for k in range(0, max_mm + 1, step):
            a.rpc("filament_recognition", {"index": idx}, timeout=3.0)
            a.keepalive(wait)
            r = a.rpc("get_filament_info", {"index": idx}, timeout=3.0)
            sku = (r or {}).get("result", {}).get("sku") if r else None
            print("  rolled %3d mm -> sku=%r" % (moved, sku), flush=True)
            if sku:
                found = sku
                break
            if moved >= max_mm:
                break
            resp = a.rpc("feed_filament",
                         {"index": idx, "length": step, "speed": speed},
                         timeout=5.0)
            print("    feed +%dmm -> %s" % (step, resp), flush=True)
            if not resp:
                break
            moved += step
            a.keepalive(step / float(speed) + 0.8)
    finally:
        if moved:
            resp = a.rpc("unwind_filament",
                         {"index": idx, "length": moved, "speed": speed},
                         timeout=5.0)
            print("  restore -%dmm -> %s" % (moved, resp), flush=True)
            a.keepalive(moved / float(speed) + 1.0)
    print("HUNT slot %d: rolled=%d mm found=%r" % (idx, moved, found), flush=True)


def mode_flash(a, imgpath, chunk=64, pace=0.01, attempts=5, dry=False, bad=False):
    data = open(imgpath, "rb").read()
    c = crc16(data)
    if bad:
        c ^= 0xFFFF
        print("!! NEGATIVE TEST: announcing deliberately WRONG crc 0x%04X" % c)
    print("image %s: %d bytes, crc16=0x%04X" % (imgpath, len(data), c))
    if dry:
        print("dry run: nothing sent")
        return
    for attempt in range(1, attempts + 1):
        print("=== attempt %d/%d ===" % (attempt, attempts), flush=True)
        sent_all = False
        try:
            if not a.prime():
                print("prime: no link yet, reopening", flush=True)
                a.reopen()
                continue
            info = a.rpc("get_info", timeout=3.0)
            print("get_info   :", info, flush=True)
            if info is None:
                a.reopen()
                continue
            ver = a.rpc("iap_version", timeout=3.0)
            print("iap_version:", ver, flush=True)
            up = a.rpc("iap_upgrade",
                       {"size": len(data), "crc": c, "version": "1.3.863"}, timeout=6.0)
            print("iap_upgrade:", up, flush=True)
            if up is None:
                a.reopen()
                continue
            base = 0x08024000
            t0 = time.time()
            for off in range(0, len(data), chunk):
                a.chunk(base + off, data[off:off + chunk])
                time.sleep(pace)
                if (off // chunk) % 256 == 0:
                    print("  %6d / %d (%.1fs)" % (off, len(data), time.time() - t0),
                          flush=True)
            print("  sent %d bytes in %.1fs" % (len(data), time.time() - t0), flush=True)
            sent_all = True
            time.sleep(0.3)
            print("iap_upgrade_finish:", a.rpc("iap_upgrade_finish", {}, timeout=10),
                  flush=True)
            for i in range(30):
                s = a.rpc("get_status", timeout=2.0)
                print("  poll %d alive=%s" % (i, s is not None), flush=True)
                time.sleep(0.5)
            return
        except Exception as e:
            if sent_all:
                print("transfer completed; post-upload disconnect is expected", flush=True)
                return
            print("attempt %d failed: %s" % (attempt, e), flush=True)
            try:
                a.reopen()
            except Exception as e2:
                print("reopen failed: %s" % e2, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    dev = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "version"
    ac = Ace(dev)
    try:
        if mode == "version":
            mode_version(ac)
        elif mode == "ping":
            mode_ping(ac, float(sys.argv[3]) if len(sys.argv) > 3 else 20.0)
        elif mode == "probe":
            mode_probe(ac, int(sys.argv[3]) if len(sys.argv) > 3 else 0)
        elif mode == "finfo":
            mode_finfo(ac)
        elif mode == "rate":
            for att in range(1, 4):
                try:
                    mode_rate(ac, int(sys.argv[3]),
                              int(sys.argv[4]) if len(sys.argv) > 4 else 10)
                    break
                except Exception as e:
                    print("rate attempt %d failed: %s" % (att, e), flush=True)
                    try:
                        ac.reopen()
                    except Exception as e2:
                        print("reopen failed: %s" % e2, flush=True)
                    time.sleep(3)
        elif mode == "hunt":
            mode_hunt(ac, int(sys.argv[3]),
                      int(sys.argv[4]) if len(sys.argv) > 4 else 60)
        elif mode == "flash":
            mode_flash(ac, sys.argv[3], dry=("--dry" in sys.argv),
                       bad=("--badcrc" in sys.argv))
    finally:
        ac.close()
