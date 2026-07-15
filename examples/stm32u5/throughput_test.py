#!/usr/bin/env python3
"""Host-to-device write throughput tester for the usb_hs_serial sink example.

The firmware reads and discards everything it receives (and logs its own RX
rate over defmt/RTT), so this script just streams data at the COM port as fast
as possible and measures the sustained write rate. Compare the number reported
here with the device's defmt log: they should agree within the size of the
Windows driver buffer.

Requires pyserial:  pip install pyserial

Usage:
    python throughput_test.py                 # auto-detect port by VID:PID c0de:cafe
    python throughput_test.py -p COM5 -t 10 -c 65536
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Run: pip install pyserial")

# VID/PID set in the example firmware: embassy_usb::Config::new(0xc0de, 0xcafe)
FIRMWARE_VID = 0xC0DE
FIRMWARE_PID = 0xCAFE


def find_port() -> str:
    matches = [p.device for p in list_ports.comports() if p.vid == FIRMWARE_VID and p.pid == FIRMWARE_PID]
    if not matches:
        available = ", ".join(p.device for p in list_ports.comports()) or "none"
        sys.exit(
            f"No device with VID:PID {FIRMWARE_VID:04x}:{FIRMWARE_PID:04x} found "
            f"(available ports: {available}). Specify one with --port."
        )
    if len(matches) > 1:
        sys.exit(f"Multiple matching devices found ({', '.join(matches)}). Specify one with --port.")
    return matches[0]


def main():
    ap = argparse.ArgumentParser(description="Measure host-to-device write throughput of the usb_hs_serial sink device.")
    ap.add_argument("-p", "--port", help="COM port (default: auto-detect by VID:PID c0de:cafe)")
    ap.add_argument("-t", "--duration", type=float, default=5.0, help="test duration in seconds (default: 5)")
    ap.add_argument("-c", "--chunk-size", type=int, default=65536, help="write chunk size in bytes (default: 65536)")
    args = ap.parse_args()

    port_name = args.port or find_port()
    chunk = bytes(i & 0xFF for i in range(256)) * (max(1, args.chunk_size // 256))

    # Baud rate is meaningless for CDC ACM (USB native speed); pyserial requires a value anyway.
    port = serial.Serial(port_name, baudrate=115200, timeout=0.1, write_timeout=5)
    try:
        port.set_buffer_size(rx_size=1 << 20, tx_size=1 << 20)
    except (AttributeError, serial.SerialException) as e:
        print(f"warning: could not set driver buffer size ({e})")
    print(f"Port: {port_name}, chunk size: {len(chunk)} bytes, duration: {args.duration:.1f}s")

    sent = 0
    last_report = start = time.monotonic()
    sent_at_report = 0
    deadline = start + args.duration
    try:
        while time.monotonic() < deadline:
            port.write(chunk)
            sent += len(chunk)
            now = time.monotonic()
            if now - last_report >= 1.0:
                rate = (sent - sent_at_report) / (now - last_report)
                print(f"  {rate / 1e6:6.2f} MB/s")
                last_report, sent_at_report = now, sent
        port.flush()  # wait for the driver to drain its buffer before stopping the clock
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except serial.SerialException as e:
        sys.exit(f"write failed: {e}")
    finally:
        elapsed = time.monotonic() - start
        port.close()

    print(f"\nElapsed: {elapsed:.2f} s")
    print(f"Sent:    {sent:,} bytes ({sent / elapsed / 1e6:.2f} MB/s)")


if __name__ == "__main__":
    main()
