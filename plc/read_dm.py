from fins.udp import UDPFinsConnection


PLC_IP = "192.168.0.20"

fins = UDPFinsConnection()

fins.dest_node_add = 20
fins.srce_node_add = 18

fins.connect(PLC_IP)


response = fins.memory_area_read(
    b'\x82',
    b'\x00\x64\x00',
    1
)

print(response)

data = response[-2:]

value = int.from_bytes(
    data,
    byteorder='big'
)

print("Raw data =", data)
print("DM100 =", value)
