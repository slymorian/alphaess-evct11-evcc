#!/usr/bin/env python3
"""
rs485_capture.py — passives Mitschneiden eines RS485-Busses über einen
USB-RS485-Dongle. Schreibt zwei Dateien:

  <out>.bin  – die rohen Bytes exakt in Empfangsreihenfolge (für den Decoder)
  <out>.csv  – ein Log mit Zeitstempel, Delta zur letzten Chunk-Ankunft,
               Byte-Anzahl und Hex-Dump je read()-Aufruf (zur groben
               Orientierung, welche Bytes zeitlich zusammengehören)

Wichtig: Es wird NIE geschrieben (ser.write()) — reines Lauschen.
Der Adapter muss also weiterhin nur an der Sniffer-Verbindung hängen,
B3 und Wallbox bleiben unabhängig davon über den Splitter verbunden.

Beispiel:
    python3 rs485_capture.py --port /dev/ttyUSB0 --baud 9600 --out capture1

Zum Stoppen: Strg+C. Danach steht eine Zusammenfassung in der Konsole.
"""
import argparse
import csv
import datetime
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("Bitte zuerst installieren: pip install pyserial")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/ttyUSB0", help="serielles Gerät des Sniffer-Dongles")
    ap.add_argument("--baud", type=int, default=9600, help="Baudrate (B3-Standard: 9600)")
    ap.add_argument("--parity", default="N", choices=["N", "E", "O"], help="Parität (8N1-Standard: N)")
    ap.add_argument("--stopbits", type=float, default=1, help="Stopbits (Standard: 1)")
    ap.add_argument("--out", default=None, help="Basisname der Ausgabedateien (ohne Endung)")
    ap.add_argument("--chunk", type=int, default=4096, help="max. Bytes pro read()-Aufruf")
    args = ap.parse_args()

    if args.out is None:
        args.out = "rs485_capture_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}

    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        bytesize=serial.EIGHTBITS,
        parity=parity_map[args.parity],
        stopbits=args.stopbits,
        timeout=0.02,  # kurzer Timeout, damit read() zeitnah zurückkehrt
    )

    bin_path = args.out + ".bin"
    csv_path = args.out + ".csv"

    print(f"Lausche auf {args.port} @ {args.baud} 8{args.parity}1 ...")
    print(f"  -> {bin_path}")
    print(f"  -> {csv_path}")
    print("Strg+C zum Beenden.\n")

    total_bytes = 0
    total_chunks = 0
    last_ts = None
    start_ts = time.perf_counter()

    with open(bin_path, "wb") as bin_f, open(csv_path, "w", newline="") as csv_f:
        writer = csv.writer(csv_f)
        writer.writerow(["iso_timestamp", "delta_ms", "num_bytes", "hex"])
        try:
            while True:
                data = ser.read(args.chunk)
                if not data:
                    continue
                now = time.perf_counter()
                iso = datetime.datetime.now().isoformat(timespec="microseconds")
                delta_ms = (now - last_ts) * 1000.0 if last_ts is not None else 0.0
                last_ts = now

                bin_f.write(data)
                bin_f.flush()
                writer.writerow([iso, f"{delta_ms:.3f}", len(data), data.hex()])
                csv_f.flush()

                total_bytes += len(data)
                total_chunks += 1
                print(f"[{delta_ms:8.2f} ms] {len(data):3d} B  {data.hex()}")
        except KeyboardInterrupt:
            pass
        finally:
            ser.close()

    dur = time.perf_counter() - start_ts
    print("\n--- Zusammenfassung ---")
    print(f"Dauer:        {dur:.1f} s")
    print(f"Chunks:       {total_chunks}")
    print(f"Bytes gesamt: {total_bytes}")
    print(f"Roh-Log:      {bin_path}")
    print(f"Zeit-Log:     {csv_path}")


if __name__ == "__main__":
    main()
