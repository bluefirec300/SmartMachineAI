from fins.udp import UDPFinsConnection

from plc.base_driver import BaseDriver


class FINSDriver(BaseDriver):
    DM_MEMORY_AREA = b"\x82"
    CIO_BIT_MEMORY_AREA = b"\x30"

    def __init__(
        self,
        ip: str,
        plc_node: int,
        pc_node: int,
        port: int = 9600,
    ):
        self.ip = ip
        self.port = port
        self.plc_node = plc_node
        self.pc_node = pc_node
        self.connection = None

    def connect(self):
        self.connection = UDPFinsConnection()

        self.connection.dest_node_add = self.plc_node
        self.connection.srce_node_add = self.pc_node

        self.connection.connect(self.ip)

        print(
            f"FINS connected to {self.ip}:{self.port} "
            f"(PLC node {self.plc_node}, PC node {self.pc_node})"
        )

    def disconnect(self):
        self.connection = None
        print("FINS disconnected.")

    def read(self, tag):
        self._ensure_connected()

        name = tag["name"]
        address = tag["address"]
        data_type = tag.get("data_type", "WORD").upper()

        value = self._read_address(
            address=address,
            data_type=data_type,
        )

        return {
            "name": name,
            "address": address,
            "value": value,
        }

    def read_all(self, tags):
        self._ensure_connected()

        values = []

        for tag in tags:
            try:
                values.append(self.read(tag))

            except Exception as error:
                print(
                    f"FINS read error for "
                    f"{tag.get('name', 'UnknownTag')} "
                    f"({tag.get('address', 'UnknownAddress')}): "
                    f"{error}"
                )

        return values

    def write(self, tag, value):
        raise NotImplementedError(
            "FINS write is not implemented yet."
        )

    def _read_address(
        self,
        address: str,
        data_type: str,
    ):
        normalized_address = address.strip().upper()

        if normalized_address.startswith("DM"):
            return self._read_dm_word(
                normalized_address,
                data_type,
            )

        if normalized_address.startswith("CIO"):
            return self._read_cio_bit(normalized_address)

        raise ValueError(
            f"Unsupported FINS address: {address}"
        )

    def _read_dm_word(
        self,
        address: str,
        data_type: str,
    ):
        dm_number_text = address.replace("DM", "", 1)

        if not dm_number_text.isdigit():
            raise ValueError(
                f"Invalid DM address: {address}"
            )

        dm_number = int(dm_number_text)

        fins_address = (
            dm_number.to_bytes(
                2,
                byteorder="big",
            )
            + b"\x00"
        )

        response = self.connection.memory_area_read(
            self.DM_MEMORY_AREA,
            fins_address,
            1,
        )

        raw_value = int.from_bytes(
            response[-2:],
            byteorder="big",
            signed=False,
        )

        if data_type in {"INT", "SIGNED", "INT16"}:
            return int.from_bytes(
                response[-2:],
                byteorder="big",
                signed=True,
            )

        if data_type in {
            "REAL",
            "FLOAT",
            "FLOAT32",
        }:
            raise NotImplementedError(
                f"32-bit floating-point reading is not "
                f"implemented yet for {address}."
            )

        return raw_value

    def _read_cio_bit(self, address: str):
        address_body = address.replace("CIO", "", 1)

        if "." not in address_body:
            raise ValueError(
                f"Invalid CIO bit address: {address}"
            )

        word_text, bit_text = address_body.split(
            ".",
            maxsplit=1,
        )

        word_number = int(word_text)
        bit_number = int(bit_text)

        if not 0 <= bit_number <= 15:
            raise ValueError(
                f"CIO bit must be from 00 to 15: {address}"
            )

        fins_address = (
            word_number.to_bytes(
                2,
                byteorder="big",
            )
            + bit_number.to_bytes(
                1,
                byteorder="big",
            )
        )

        response = self.connection.memory_area_read(
            self.CIO_BIT_MEMORY_AREA,
            fins_address,
            1,
        )

        return int(response[-1])

    def _ensure_connected(self):
        if self.connection is None:
            raise ConnectionError(
                "FINS driver is not connected."
            )
