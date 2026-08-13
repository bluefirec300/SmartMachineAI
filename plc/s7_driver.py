"""
Siemens S7 driver (S7-300/400/1200/1500 via python-snap7).

Address format - standard Siemens DB (data block) notation, same as
you'd see in TIA Portal / Step 7:

    DB<db_number>.DBX<byte>.<bit>   - BOOL (a single bit)
    DB<db_number>.DBW<byte>         - INT (16-bit signed word)
    DB<db_number>.DBD<byte>         - REAL (32-bit float) or DINT,
                                       controlled by the tag's own
                                       data_type

e.g. "DB1.DBX0.0" (bit 0 of byte 0 in DB1), "DB1.DBD4" (a REAL at
byte offset 4 in DB1).

STRING tags aren't supported over S7 in this driver (S7 strings have
their own length-prefixed encoding that would need a distinct address
convention) - same honest "not implemented yet" stance as the other
drivers for what they don't cover.
"""

from __future__ import annotations

import re

from plc.base_driver import BaseDriver

_ADDRESS_PATTERN = re.compile(
    r"^DB(?P<db>\d+)\.DB(?P<kind>[XWD])(?P<byte>\d+)(?:\.(?P<bit>\d+))?$",
    re.IGNORECASE,
)


class S7Driver(BaseDriver):
    def __init__(self, ip: str, rack: int = 0, slot: int = 1, port: int = 102):
        self.ip = ip
        self.rack = rack
        self.slot = slot
        self.port = port
        self.client = None

    def connect(self):
        from snap7.client import Client

        self.client = Client()
        self.client.connect(self.ip, self.rack, self.slot, self.port)

        print(f"S7 connected to {self.ip}:{self.port} (rack {self.rack}, slot {self.slot})")

    def disconnect(self):
        if self.client is not None:
            self.client.disconnect()
        self.client = None
        print("S7 disconnected.")

    def read(self, tag):
        self._ensure_connected()

        name = tag["name"]
        address = tag["address"]
        data_type = tag.get("data_type", "REAL").upper()

        value = self._read_address(address, data_type)

        return {"name": name, "address": address, "value": value}

    def read_all(self, tags):
        self._ensure_connected()

        values = []

        for tag in tags:
            try:
                values.append(self.read(tag))
            except Exception as error:
                print(
                    f"S7 read error for {tag.get('name', 'UnknownTag')} "
                    f"({tag.get('address', 'UnknownAddress')}): {error}"
                )

        return values

    def write(self, tag, value):
        raise NotImplementedError("S7 write is not implemented yet.")

    def _parse_address(self, address: str) -> tuple[int, str, int, int | None]:
        match = _ADDRESS_PATTERN.match(address.strip())

        if not match:
            raise ValueError(
                f"Invalid S7 address: {address!r} (expected e.g. 'DB1.DBD4' or 'DB1.DBX0.0')"
            )

        db_number = int(match.group("db"))
        kind = match.group("kind").upper()
        byte_offset = int(match.group("byte"))
        bit_offset = int(match.group("bit")) if match.group("bit") is not None else None

        if kind == "X" and bit_offset is None:
            raise ValueError(f"DBX (bit) address requires a bit index: {address!r}")

        return db_number, kind, byte_offset, bit_offset

    def _read_address(self, address: str, data_type: str):
        from snap7 import util

        db_number, kind, byte_offset, bit_offset = self._parse_address(address)

        if kind == "X":
            data = self.client.db_read(db_number, byte_offset, 1)
            return bool(util.get_bool(data, 0, bit_offset))

        if kind == "W":
            data = self.client.db_read(db_number, byte_offset, 2)
            return util.get_int(data, 0)

        if kind == "D":
            data = self.client.db_read(db_number, byte_offset, 4)

            if data_type in ("REAL", "FLOAT", "FLOAT32"):
                return util.get_real(data, 0)

            return util.get_dint(data, 0)

        raise ValueError(f"Unsupported S7 address kind: {kind!r} in {address!r}")

    def _ensure_connected(self):
        if self.client is None:
            raise ConnectionError("S7 driver is not connected.")
