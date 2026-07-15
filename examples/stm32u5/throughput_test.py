#!/usr/bin/env python3
"""Throughput tester for the usb_hs_serial echo example.

The firmware echoes back every packet it receives, so this script measures
round-trip throughput: a writer thread streams data to the COM port while the
main thread reads the echo back and counts bytes. With --verify, the echoed
data is checked against the transmitted pattern.

Requires pyserial:  pip install pyserial

Usage:
    python throughput_test.py                 # auto-detect port by VID:PID c0de:cafe
    python throughput_test.py -p COM5 -t 10
    python throughput_test.py --chunk-size 16384 --verify
"""

import argparse
import sys
import threading
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


def make_pattern(length: int) -> bytes:
    """Deterministic repeating pattern so the receiver can verify the echo at any offset."""
    return bytes(i & 0xFF for i in range(length))


def writer(port: serial.Serial, chunk: bytes, deadline: float, stats: dict, stop: threading.Event):
    sent = 0
    try:
        while time.monotonic() < deadline and not stop.is_set():
            port.write(chunk)
            sent += len(chunk)
    except serial.SerialException as e:
        stats["error"] = f"write failed: {e}"
        stop.set()
    finally:
        stats["sent"] = sent


def main():
    ap = argparse.ArgumentParser(description="Measure round-trip throughput of the usb_hs_serial echo device.")
    ap.add_argument("-p", "--port", help="COM port (default: auto-detect by VID:PID c0de:cafe)")
    ap.add_argument("-t", "--duration", type=float, default=5.0, help="test duration in seconds (default: 5)")
    ap.add_argument("-c", "--chunk-size", type=int, default=8192, help="write chunk size in bytes (default: 8192)")
    ap.add_argument("--verify", action="store_true", help="verify echoed data matches the transmitted pattern")
    args = ap.parse_args()

    port_name = args.port or find_port()
    pattern = make_pattern(256)
    # Chunk is a whole number of pattern repetitions so the echo stream is
    # continuous and verifiable purely from the received byte count.
    reps = max(1, args.chunk_size // len(pattern))
    chunk = pattern * reps

    # Baud rate is meaningless for CDC ACM (USB native speed); pyserial requires a value anyway.
    port = serial.Serial(port_name, baudrate=115200, timeout=0.1, write_timeout=2)
    print(f"Port: {port_name}, chunk size: {len(chunk)} bytes, duration: {args.duration:.1f}s")

    port.reset_input_buffer()
    stats = {"sent": 0}
    stop = threading.Event()
    deadline = time.monotonic() + args.duration
    tx = threading.Thread(target=writer, args=(port, chunk, deadline, stats, stop), daemon=True)

    received = 0
    errors = 0
    start = time.monotonic()
    tx.start()
    try:
        # Read while the writer runs, then drain the remaining echo backlog.
        drain_deadline = None
        while True:
            data = port.read(65536)
            if data:
                if args.verify:
                    # The stream is the 256-byte pattern repeating, so the expected
                    # bytes at any offset are a slice of it. Compare in bulk.
                    off = received % len(pattern)
                    expected = (pattern * (len(data) // len(pattern) + 2))[off : off + len(data)]
                    if data != expected:
                        errors += sum(a != b for a, b in zip(data, expected))
                received += len(data)
            if stop.is_set():
                break
            if not tx.is_alive():
                if drain_deadline is None:
                    drain_deadline = time.monotonic() + 1.0
                if not data and (received >= stats["sent"] or time.monotonic() > drain_deadline):
                    break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        stop.set()
        tx.join(timeout=3)
        elapsed = time.monotonic() - start
        port.close()

    if "error" in stats:
        print(f"ERROR: {stats['error']}")

    sent = stats["sent"]
    print(f"\nElapsed:   {elapsed:.2f} s")
    print(f"Sent:      {sent:,} bytes ({sent / elapsed / 1e6:.2f} MB/s)")
    print(f"Received:  {received:,} bytes ({received / elapsed / 1e6:.2f} MB/s)")
    if sent:
        print(f"Echoed:    {received / sent * 100:.1f}% of sent data")
    if args.verify:
        print(f"Integrity: {'OK' if errors == 0 else f'{errors:,} byte mismatches'}")
    if received == 0:
        sys.exit("No data echoed back — is the firmware running and connected?")


if __name__ == "__main__":
    main()
