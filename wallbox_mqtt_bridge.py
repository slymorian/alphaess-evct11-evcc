#!/usr/bin/env python3
"""
wallbox_mqtt_bridge.py — Verbindet unseren reverse-engineerten Modbus-Master
(spricht die Wallbox direkt an, Rolle wie B3) mit den bestehenden EVCC-MQTT-
Topics aus evcc.yaml:

  Wir PUBLIZIEREN (lesen von der Wallbox):
    <prefix>/status    "A" | "B" | "C"
    <prefix>/enabled   "true" | "false"
    <prefix>/power     aktuelle Ladeleistung in Watt (Zusatzinfo)

  Wir ABONNIEREN (schreiben an die Wallbox):
    <prefix>/set/enabled   "true"/"false" (payload aus EVCCs {{ .enable }})
    <prefix>/maxcurrent    Zahl (Ampere pro Phase)

Ersetzt die bisherige Cloud->HA-Integration->Node-RED-Kette für die Wallbox-
Daten, die nach Entfernung der Wallbox aus der B3-Konfiguration nicht mehr
funktionieren würde (die Cloud bekäme dann keine Wallbox-Daten mehr von B3).

Umrechnung Ampere -> Rohregister (0x0101), empirisch kalibriert:
    R1 = 652 * Ampere + 1239   (gültig ca. 6-15A)
    R1 <= ~4200                -> keine Ladung
    R1 = 0                     -> definitiv aus (für enable=false)

=======================================================================
SICHERHEITSHINWEIS
=======================================================================
Dieses Skript sendet AKTIV auf dem RS485-Bus (wie b3_master_emulator.py).
Nur verwenden, wenn die echte B3 physisch vom Bus getrennt ist!
=======================================================================

Beispiel:
    python3 wallbox_mqtt_bridge.py \\
        --port /dev/ttyUSB0 --baud 9600 \\
        --mqtt-host xxx.xxx.x.xxx --mqtt-port 1883 \\
        --mqtt-user DEIN_USER --mqtt-pass DEIN_PASSWORT \\
        --mqtt-prefix alphaess/wallbox
"""
import argparse
import os
import sys
import time
import threading

try:
    import serial
except ImportError:
    sys.exit("Bitte zuerst installieren: apt install -y python3-serial")

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("Bitte zuerst installieren: pip install paho-mqtt --break-system-packages")


# ---------------------------------------------------------------------------
# Modbus-Grundfunktionen (identisch zu b3_master_emulator.py)
# ---------------------------------------------------------------------------

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


def with_crc(body: bytes) -> bytes:
    crc = modbus_crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_read_request(unit_id: int, addr: int, qty: int) -> bytes:
    body = bytes([unit_id, 0x03]) + addr.to_bytes(2, 'big') + qty.to_bytes(2, 'big')
    return with_crc(body)


def build_write_request(unit_id: int, start_addr: int, regs: list) -> bytes:
    data = bytearray()
    for r in regs:
        data += int(r).to_bytes(2, 'big', signed=False)
    body = (bytes([unit_id, 0x10]) + start_addr.to_bytes(2, 'big') +
            len(regs).to_bytes(2, 'big') + bytes([len(data)]) + bytes(data))
    return with_crc(body)


def try_extract_frame(buf: bytes, unit_id: int):
    """Erkennt Read-Response oder Write-ACK. Rückgabe: (status, (len, kind, info))."""
    n = len(buf)
    if n < 1:
        return 'incomplete', None
    if buf[0] != unit_id:
        return 'discard1', None
    if n < 2:
        return 'incomplete', None
    func = buf[1]

    if func == 0x03:
        if n < 3:
            return 'incomplete', None
        bc = buf[2]
        total = 3 + bc + 2
        if n < total:
            return 'incomplete', None
        frame = buf[:total]
        if modbus_crc16(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
            return 'discard1', None
        data = frame[3:3 + bc]
        regs = [int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data), 2)]
        return 'ok', (total, 'read_response', regs)

    if func == 0x10:
        if n >= 8:
            short = buf[:8]
            if modbus_crc16(short[:-2]) == (short[-2] | (short[-1] << 8)):
                return 'ok', (8, 'write_ack', None)
        if n < 7:
            return 'incomplete', None
        bc = buf[6]
        total = 7 + bc + 2
        if n < total:
            return 'incomplete', None
        frame = buf[:total]
        if modbus_crc16(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
            return 'discard1', None
        return 'ok', (total, 'write_request', None)

    return 'discard1', None


# ---------------------------------------------------------------------------
# Umrechnung Ampere <-> Rohregister
# ---------------------------------------------------------------------------

ACTIVATION_THRESHOLD = 4300  # empirisch: <=4200 aus, >=4300 an
R357_DEFAULT = (4000, 7000, 7000)  # feste Werte für R3/R5/R7 (Bedeutung ungeklärt)
CONNECTED_REG_THRESHOLD = 50  # Register 16 (0xA1): ~8 = nicht verbunden, ~144-161 = verbunden


def amps_to_r1(amps: float) -> int:
    if amps <= 0:
        return 0
    r1 = round(652 * amps + 1239)
    return max(r1, ACTIVATION_THRESHOLD)  # nie unterhalb der Aktivierungsschwelle, wenn >0 gewünscht


# ---------------------------------------------------------------------------
# Gemeinsamer Zustand zwischen Modbus-Thread und MQTT-Thread
# ---------------------------------------------------------------------------

class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        # Von der Wallbox gelesen:
        self.current_a = 0.0
        self.power_w = 0
        self.connected = False  # aus Register 16 (0xA1) abgeleitet, siehe CONNECTED_REG_THRESHOLD
        self.last_raw_regs = []  # alle 19 Rohwerte, für Diagnose
        self.last_read_ts = 0.0
        # Von EVCC gewünscht (über MQTT):
        self.enabled_wanted = False
        self.maxcurrent_a = 6.0  # Default-Startwert, falls EVCC noch nichts gesendet hat
        # Statistik
        self.reads_ok = 0
        self.writes_ok = 0

    def current_write_regs(self):
        with self.lock:
            if not self.enabled_wanted:
                r1 = 0
            else:
                r1 = amps_to_r1(self.maxcurrent_a)
        r3, r5, r7 = R357_DEFAULT
        return [0, r1, 0, r3, 0, r5, 0, r7, 0]

    def status_letter(self):
        with self.lock:
            connected = self.connected
            power = self.power_w
        if not connected:
            return 'A'  # nicht verbunden (Register 16 niedrig)
        elif power > 100:
            return 'C'  # verbunden UND lädt
        else:
            return 'B'  # verbunden, lädt aber gerade nicht

    def is_enabled(self):
        with self.lock:
            return self.enabled_wanted


# ---------------------------------------------------------------------------
# Modbus-Thread: pollt die Wallbox, schreibt den aktuell gewünschten Sollwert
# ---------------------------------------------------------------------------

def modbus_loop(state: SharedState, args, stop_event: threading.Event):
    ser = serial.Serial(
        port=args.port, baudrate=args.baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=1,
        timeout=0.02,
    )
    print(f"[Modbus] Verbunden auf {args.port} @ {args.baud} 8N1")

    buf = bytearray()
    last_poll = 0.0
    last_write = 0.0
    awaiting = None

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            data = ser.read(256)
            if data:
                buf.extend(data)

            while buf:
                status, payload = try_extract_frame(bytes(buf), args.unit_id)
                if status == 'incomplete':
                    break
                if status == 'discard1':
                    del buf[0]
                    continue
                frame_len, kind, info = payload
                if kind == 'read_response' and info and len(info) == 19:
                    with state.lock:
                        state.current_a = info[1] / 10.0
                        state.power_w = info[3]
                        state.connected = info[16] > CONNECTED_REG_THRESHOLD
                        state.last_raw_regs = list(info)
                        state.last_read_ts = now
                        state.reads_ok += 1
                    awaiting = None
                elif kind == 'write_ack':
                    state.writes_ok += 1
                    awaiting = None
                del buf[:frame_len]

            if awaiting is not None and (now - awaiting) > 0.3:
                awaiting = None

            if awaiting is None:
                if now - last_write >= args.write_interval:
                    regs = state.current_write_regs()
                    ser.write(build_write_request(args.unit_id, 0x0100, regs))
                    last_write = now
                    awaiting = now
                elif now - last_poll >= args.poll_interval:
                    ser.write(build_read_request(args.unit_id, 0x0091, 19))
                    last_poll = now
                    awaiting = now
    finally:
        ser.close()
        print("[Modbus] Verbindung geschlossen.")


# ---------------------------------------------------------------------------
# MQTT: publiziert Status, empfängt Steuerbefehle
# ---------------------------------------------------------------------------

def build_mqtt_client(args, state: SharedState):
    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"[MQTT] Verbunden mit {args.mqtt_host}:{args.mqtt_port}")
            client.subscribe(f"{args.mqtt_prefix}/set/enabled")
            client.subscribe(f"{args.mqtt_prefix}/maxcurrent")
        else:
            print(f"[MQTT] Verbindung fehlgeschlagen, rc={rc}", file=sys.stderr)

    def on_message(client, userdata, msg):
        payload = msg.payload.decode(errors='replace').strip()
        if msg.topic == f"{args.mqtt_prefix}/set/enabled":
            wanted = payload.lower() in ('true', '1', 'on', 'yes')
            with state.lock:
                state.enabled_wanted = wanted
            print(f"[MQTT] set/enabled empfangen: {payload!r} -> enabled_wanted={wanted}")
        elif msg.topic == f"{args.mqtt_prefix}/maxcurrent":
            try:
                amps = float(payload)
                with state.lock:
                    state.maxcurrent_a = amps
                print(f"[MQTT] maxcurrent empfangen: {amps}A -> R1={amps_to_r1(amps)}")
            except ValueError:
                print(f"[WARN] Ungültiger maxcurrent-Payload: {payload!r}", file=sys.stderr)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, 'CallbackAPIVersion') else mqtt.Client()
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_pass)
    client.on_connect = on_connect
    client.on_message = on_message
    return client


def mqtt_publish_loop(client, state: SharedState, args, stop_event: threading.Event):
    last_status = None
    last_enabled = None
    last_power_pub = 0.0
    while not stop_event.is_set():
        status = state.status_letter()
        enabled = state.is_enabled()
        with state.lock:
            power = state.power_w

        if status != last_status:
            client.publish(f"{args.mqtt_prefix}/status", status, retain=True)
            last_status = status
        if enabled != last_enabled:
            client.publish(f"{args.mqtt_prefix}/enabled", "true" if enabled else "false", retain=True)
            last_enabled = enabled
        now = time.monotonic()
        if now - last_power_pub >= 2.0:
            client.publish(f"{args.mqtt_prefix}/power", power)
            last_power_pub = now

        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--unit-id", type=int, default=1)
    ap.add_argument("--poll-interval", type=float, default=0.2)
    ap.add_argument("--write-interval", type=float, default=2.0)
    ap.add_argument("--mqtt-host", required=True)
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default=None,
                     help="Alternativ: Umgebungsvariable MQTT_USER (sicherer, da nicht in der Prozessliste sichtbar)")
    ap.add_argument("--mqtt-pass", default=None,
                     help="Alternativ: Umgebungsvariable MQTT_PASS (sicherer, da nicht in der Prozessliste sichtbar)")
    ap.add_argument("--mqtt-prefix", default="alphaess/wallbox")
    args = ap.parse_args()

    # Umgebungsvariablen als Fallback, falls nicht per Kommandozeile übergeben
    if args.mqtt_user is None:
        args.mqtt_user = os.environ.get("MQTT_USER")
    if args.mqtt_pass is None:
        args.mqtt_pass = os.environ.get("MQTT_PASS")
    if not args.mqtt_user or not args.mqtt_pass:
        sys.exit("MQTT-Zugangsdaten fehlen: --mqtt-user/--mqtt-pass angeben oder "
                 "MQTT_USER/MQTT_PASS als Umgebungsvariablen setzen.")

    print("=" * 70)
    print("WALLBOX-MQTT-BRÜCKE — SICHERHEITSHINWEIS")
    print("Nur verwenden, wenn die echte B3 physisch vom Bus getrennt ist!")
    print("=" * 70)

    state = SharedState()
    stop_event = threading.Event()

    mqtt_client = build_mqtt_client(args, state)
    mqtt_client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
    mqtt_client.loop_start()

    modbus_thread = threading.Thread(target=modbus_loop, args=(state, args, stop_event), daemon=True)
    modbus_thread.start()

    publish_thread = threading.Thread(target=mqtt_publish_loop, args=(mqtt_client, state, args, stop_event), daemon=True)
    publish_thread.start()

    print(f"\nBrücke läuft. MQTT-Prefix: {args.mqtt_prefix}")
    print("Strg+C zum Beenden.\n")

    try:
        while True:
            time.sleep(5)
            with state.lock:
                print(f"[Status] Verbunden={state.connected} Strom={state.current_a:.1f}A Leistung={state.power_w}W "
                      f"enabled_wanted={state.enabled_wanted} maxcurrent={state.maxcurrent_a}A "
                      f"reads={state.reads_ok} writes={state.writes_ok}")
                print(f"         Rohregister (0x91-0xA3): {state.last_raw_regs}")
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        stop_event.set()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        modbus_thread.join(timeout=2)


if __name__ == "__main__":
    main()
