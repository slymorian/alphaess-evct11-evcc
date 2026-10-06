#!/usr/bin/env bash
#
# b3_tcp_probe.sh — Minimaltest: antwortet der Smile-B3 über den
# RS485-Ethernet-Konverter?
#
# Schickt genau EINE Modbus-RTU-Leseanfrage (Funktion 03, Register 0x0102 =
# Batterie-SOC) roh über TCP an den Konverter und zeigt die Antwort. Braucht
# nur bash, od, dd und timeout — kein Python, kein EVCC. Gedacht als erster
# Test, BEVOR man EVCC auf den Netzwerkweg umstellt.
#
# Voraussetzungen:
#   - Konverter im Modus "TCP Server", transparente Übertragung (kein
#     "Modbus TCP to RTU"), seriell 9600 8N1
#   - B3 ist Modbus-SLAVE mit Adresse 85 (0x55) — siehe README, Teil B
#   - Währenddessen fragt nichts anderes den B3 ab (EVCC kurz stoppen),
#     sonst können sich Anfragen und Antworten auf dem Bus überlagern
#
# Aufruf:
#   ./b3_tcp_probe.sh <IP-des-Konverters> [Port]      (Port-Standard: 4196)
#
# Die Slave-Adresse ist hier fest 0x55, weil die CRC der Anfrage fest
# eingetragen ist. Für andere Adressen oder weitere Register:
# b3_read_test.py verwenden.

set -u

HOST="${1:-}"
PORT="${2:-4196}"

if [ -z "$HOST" ]; then
    echo "Aufruf: $0 <IP-des-Konverters> [Port]" >&2
    exit 2
fi

# CRC16 (Modbus) über eine Liste von Hex-Bytes, Ergebnis als "lo hi"
crc16() {
    local crc=$((0xFFFF)) b i
    for b in "$@"; do
        crc=$((crc ^ 16#$b))
        for i in 1 2 3 4 5 6 7 8; do
            if [ $((crc & 1)) -eq 1 ]; then
                crc=$(((crc >> 1) ^ 0xA001))
            else
                crc=$((crc >> 1))
            fi
        done
    done
    printf '%02x %02x' $((crc & 0xFF)) $((crc >> 8))
}

# Erst prüfen, ob der Port überhaupt erreichbar ist (mit Zeitlimit, sonst
# hängt /dev/tcp bei einer falschen IP sehr lange).
if ! timeout 3 bash -c "exec 3<>/dev/tcp/$HOST/$PORT" 2>/dev/null; then
    echo "FEHLER: $HOST:$PORT ist nicht erreichbar (IP/Port prüfen, Konverter im TCP-Server-Modus?)." >&2
    exit 1
fi

exec 3<>"/dev/tcp/$HOST/$PORT" || exit 1

echo "Anfrage : 55 03 01 02 00 01 29 e2   (Slave 0x55, Lesen, Register 0x0102, 1 Register)"
printf '\x55\x03\x01\x02\x00\x01\x29\xe2' >&3

# Erwartet werden 7 Bytes: Adresse, Funktion, Byte-Anzahl, 2 Datenbytes, 2 CRC
RESP=$(timeout 2 dd bs=1 count=7 <&3 2>/dev/null | od -An -tx1 | tr -s ' \n' ' ')
exec 3>&-

# shellcheck disable=SC2086
set -- $RESP
if [ $# -gt 0 ]; then echo "Antwort : $*"; else echo "Antwort : (nichts)"; fi

if [ $# -eq 0 ]; then
    cat >&2 <<'EOF'
FEHLER: keine Antwort. Typische Ursachen:
  - A/B-Adern am Konverter vertauscht (einfach tauschen und erneut testen)
  - B3 ist (noch) Modbus-Master statt Slave (siehe README, Teil B)
  - Konverter nicht auf 9600 8N1 oder nicht im transparenten Modus
  - falscher Kanal/falsche IP des Konverters
EOF
    exit 1
fi

if [ $# -lt 7 ]; then
    echo "FEHLER: unvollständige Antwort ($# von 7 Bytes)." >&2
    exit 1
fi

if [ "$(crc16 "$1" "$2" "$3" "$4" "$5")" != "$6 $7" ]; then
    echo "FEHLER: CRC stimmt nicht — Antwort verfälscht (Baudrate/Parität? A/B? zweiter Master auf dem Bus?)." >&2
    exit 1
fi

if [ "$1" != "55" ] || [ "$2" != "03" ] || [ "$3" != "02" ]; then
    echo "FEHLER: gültiger Frame, aber nicht die erwartete Antwort (Modbus-Fehlercode oder anderes Gerät?)." >&2
    exit 1
fi

RAW=$((16#$4$5))
echo "OK: CRC gültig, SOC = $((RAW / 10)).$((RAW % 10)) %  (Rohwert $RAW, Skalierung 0,1 %)"
