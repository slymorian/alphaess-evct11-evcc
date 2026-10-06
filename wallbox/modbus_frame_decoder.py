#!/usr/bin/env python3
"""
modbus_frame_decoder.py — zerlegt eine mit rs485_capture.py aufgezeichnete
.bin-Datei in einzelne Modbus-RTU-Frames.

Da Timing-basierte Frame-Erkennung (3.5-Zeichen-Stille laut Modbus-Spec)
bei billigen USB-RS485-Adaptern wegen interner Latenz-Timer unzuverlässig
ist, arbeitet dieser Decoder stattdessen mit einem CRC16-Scan: an jeder
Byte-Position wird anhand von Adresse + Funktionscode die *erwartete*
Frame-Länge berechnet (getrennt für die Anfrage- und die Antwort-Form,
da beide im rohen Bytestrom ununterscheidbar aufeinanderfolgen) und per
CRC16 (Modbus-Polynom) verifiziert. Nur bei gültiger CRC wird ein Frame
akzeptiert; sonst rückt der Scan um 1 Byte weiter (Rauschen/Kollision).

Verwendung:
    python3 modbus_frame_decoder.py capture1.bin
    python3 modbus_frame_decoder.py capture1.bin --summary
"""
import argparse
import sys
from collections import Counter, defaultdict

FUNC_NAMES = {
    1: "Read Coils",
    2: "Read Discrete Inputs",
    3: "Read Holding Registers",
    4: "Read Input Registers",
    5: "Write Single Coil",
    6: "Write Single Register",
    15: "Write Multiple Coils",
    16: "Write Multiple Registers",
    23: "Read/Write Multiple Registers",
}


def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def crc_ok(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    calc = modbus_crc16(frame[:-2])
    given = frame[-2] | (frame[-1] << 8)
    return calc == given


def candidate_lengths(buf: bytes, i: int):
    """Liefert mögliche Frame-Längen (mit Herkunftshinweis) ab Position i,
    basierend auf Funktionscode-Layout. Mehrere Kandidaten möglich, da
    Anfrage/Antwort im Bytestrom nicht direkt markiert sind."""
    if i + 2 > len(buf):
        return []
    func_raw = buf[i + 1]
    func = func_raw & 0x7F
    out = []

    if func_raw & 0x80:
        out.append((5, "exception"))
        return out

    if func in (1, 2, 3, 4):
        out.append((8, "request (fix)"))
        if i + 3 <= len(buf):
            bc = buf[i + 2]
            out.append((3 + bc + 2, "response (byteCount)"))
    elif func in (5, 6):
        out.append((8, "request/response (echo)"))
    elif func in (15, 16):
        out.append((8, "response (echo start+qty)"))
        if i + 7 <= len(buf):
            bc = buf[i + 6]
            out.append((7 + bc + 2, "request (byteCount)"))
    elif func == 23:
        if i + 3 <= len(buf):
            bc = buf[i + 2]
            out.append((3 + bc + 2, "response (byteCount)"))
        if i + 11 <= len(buf):
            bc = buf[i + 10]
            out.append((11 + bc + 2, "request (byteCount)"))
    else:
        # unbekannter Funktionscode: heuristisch kurze Längen probieren
        for length in range(4, 33):
            out.append((length, "heuristic"))

    return out


def extract_addr(frame: bytes, func: int, kind: str):
    """Extrahiert die Start-/Zieladresse als int, sofern aus dem Frame ableitbar
    (für die Gruppierung in der Zusammenfassung nach Registerbereich).
    Nur bei Frames, bei denen Offset 2-3 tatsächlich die Adresse enthält
    (Requests fester Länge, Write-Echo-Antworten, Write-Single-Anfragen/-Antworten) —
    bei Byte-Count-Responses (03/04-Antwort) steht dort keine Adresse."""
    try:
        if len(frame) < 4:
            return None
        if func in (1, 2, 3, 4) and "request" in kind:
            return (frame[2] << 8) | frame[3]
        if func in (5, 6):
            return (frame[2] << 8) | frame[3]
        if func in (15, 16):
            return (frame[2] << 8) | frame[3]
    except IndexError:
        pass
    return None


def decode_fields(frame: bytes, func: int, kind: str) -> str:
    try:
        if func in (1, 2, 3, 4) and "request" in kind:
            addr = (frame[2] << 8) | frame[3]
            qty = (frame[4] << 8) | frame[5]
            return f"start=0x{addr:04X} qty={qty}"
        if func in (1, 2, 3, 4) and "response" in kind:
            bc = frame[2]
            data = frame[3:3 + bc]
            return f"bytes={bc} data={data.hex()}"
        if func in (5, 6):
            addr = (frame[2] << 8) | frame[3]
            val = (frame[4] << 8) | frame[5]
            return f"addr=0x{addr:04X} value=0x{val:04X} ({val})"
        if func in (15, 16) and "request" in kind:
            addr = (frame[2] << 8) | frame[3]
            qty = (frame[4] << 8) | frame[5]
            bc = frame[6]
            data = frame[7:7 + bc]
            return f"start=0x{addr:04X} qty={qty} data={data.hex()}"
        if func in (15, 16) and "response" in kind:
            addr = (frame[2] << 8) | frame[3]
            qty = (frame[4] << 8) | frame[5]
            return f"start=0x{addr:04X} qty={qty}"
        if kind == "exception":
            return f"exception_code=0x{frame[2]:02X}"
    except IndexError:
        pass
    return frame.hex()


def scan(buf: bytes):
    frames = []
    i = 0
    noise = 0
    n = len(buf)
    while i < n:
        best = None
        for length, kind in candidate_lengths(buf, i):
            if length < 4 or i + length > n:
                continue
            frame = buf[i:i + length]
            if crc_ok(frame):
                best = (length, kind, frame)
                break  # erste passende Interpretation nehmen
        if best:
            length, kind, frame = best
            unit_id = frame[0]
            func_raw = frame[1]
            func = func_raw & 0x7F
            fname = FUNC_NAMES.get(func, f"func=0x{func:02X}")
            fields = decode_fields(frame, func, kind)
            addr = extract_addr(frame, func, kind)
            frames.append({
                "offset": i,
                "unit_id": unit_id,
                "func": func,
                "func_name": fname,
                "kind": kind,
                "fields": fields,
                "hex": frame.hex(),
                "len": length,
                "addr": addr,
            })
            i += length
        else:
            noise += 1
            i += 1
    return frames, noise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("infile", help=".bin-Datei aus rs485_capture.py")
    ap.add_argument("--summary", action="store_true", help="zusätzlich Häufigkeits-/Write-Übersicht ausgeben")
    ap.add_argument("--max", type=int, default=None, help="max. Anzahl Frames ausgeben")
    args = ap.parse_args()

    with open(args.infile, "rb") as f:
        buf = f.read()

    frames, noise = scan(buf)

    print(f"Eingelesen: {len(buf)} Bytes")
    print(f"Erkannte Frames: {len(frames)}   |   verworfene Rausch-Bytes: {noise}\n")

    shown = frames if args.max is None else frames[:args.max]
    for fr in shown:
        print(f"@{fr['offset']:6d}  id={fr['unit_id']:3d} (0x{fr['unit_id']:02X})  "
              f"{fr['func_name']:26s} [{fr['kind']:22s}]  {fr['fields']:40s}  raw={fr['hex']}")

    if args.summary:
        print("\n--- Häufigkeit pro (unit_id, Funktion, Art, Adresse) ---")
        counter = Counter((fr["unit_id"], fr["func_name"], fr["kind"], fr["addr"]) for fr in frames)
        for (uid, fname, kind, addr), cnt in counter.most_common():
            addr_s = f"0x{addr:04X}" if addr is not None else "n/a"
            print(f"  {cnt:5d}x  id={uid:3d}  {fname:26s} [{kind:22s}]  addr={addr_s}")

        print("\n--- Alle Schreib-Frames (Funktion 5/6/15/16) — vermutlich Steuerbefehle ---")
        writes = [fr for fr in frames if fr["func"] in (5, 6, 15, 16)]
        if not writes:
            print("  (keine gefunden)")
        for fr in writes:
            print(f"  @{fr['offset']:6d}  id={fr['unit_id']:3d}  {fr['func_name']:24s} "
                  f"{fr['fields']:40s}  raw={fr['hex']}")


if __name__ == "__main__":
    main()
