"""
Modbus TCP driver.

Address format (case-insensitive, set per-tag in tag_addresses.address,
same mapping mechanism already used for the simulator/FINS drivers):

    HR:<register>   - holding register (INT: 1 register: REAL: 2
                       registers, big-endian IEEE-754 float)
    IR:<register>   - input register (read-only, same width rules as HR)
    COIL:<register> - coil (BOOL, 1 bit, read/write)
    DI:<register>   - discrete input (BOOL, 1 bit, read-only)

e.g. "HR:100" (holding register 100), "COIL:12".

STRING tags aren't supported over Modbus in this driver (no
established convention here for which registers encode text) - same
honest "not implemented yet" stance as FINSDriver.write().
"""

from __future__ import annotations

import struct

from plc.base_driver import BaseDriver


class ModbusDriver(BaseDriver):
    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 3):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.client = None

    def connect(self):
        from pymodbus.client import ModbusTcpClient

        self.client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)

        if not self.client.connect():
            raise ConnectionError(f"Could not connect to Modbus TCP {self.host}:{self.port}")

        print(f"Modbus TCP connected to {self.host}:{self.port} (unit {self.unit_id})")

    def disconnect(self):
        if self.client is not None:
            self.client.close()
        self.client = None
        print("Modbus TCP disconnected.")

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
                    f"Modbus read error for {tag.get('name', 'UnknownTag')} "
                    f"({tag.get('address', 'UnknownAddress')}): {error}"
                )

        return values

    def write(self, tag, value):
        raise NotImplementedError("Modbus write is not implemented yet.")

    def _parse_address(self, address: str) -> tuple[str, int]:
        kind, _, register_text = address.strip().upper().partition(":")

        if not register_text or not register_text.isdigit():
            raise ValueError(f"Invalid Modbus address: {address!r} (expected e.g. 'HR:100')")

        return kind, int(register_text)

    def _read_address(self, address: str, data_type: str):
        kind, register = self._parse_address(address)

        if kind == "COIL":
            result = self.client.read_coils(register, count=1, device_id=self.unit_id)
            self._raise_if_error(result)
            return int(result.bits[0])

        if kind == "DI":
            result = self.client.read_discrete_inputs(register, count=1, device_id=self.unit_id)
            self._raise_if_error(result)
            return int(result.bits[0])

        if kind in ("HR", "IR"):
            register_count = 2 if data_type in ("REAL", "FLOAT", "FLOAT32") else 1
            reader = self.client.read_holding_registers if kind == "HR" else self.client.read_input_registers
            result = reader(register, count=register_count, device_id=self.unit_id)
            self._raise_if_error(result)

            if register_count == 2:
                raw = struct.pack(">HH", *result.registers)
                return struct.unpack(">f", raw)[0]

            raw_value = result.registers[0]

            if data_type in ("INT", "SIGNED", "INT16"):
                return raw_value - 0x10000 if raw_value >= 0x8000 else raw_value

            return raw_value

        raise ValueError(f"Unsupported Modbus address kind: {kind!r} in {address!r}")

    @staticmethod
    def _raise_if_error(result) -> None:
        if result.isError():
            raise IOError(f"Modbus error response: {result}")

    def _ensure_connected(self):
        if self.client is None:
            raise ConnectionError("Modbus driver is not connected.")
