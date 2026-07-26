from fins.udp import UDPFinsConnection


PLC_IP = "192.168.0.20"

fins = UDPFinsConnection()

# PLC FINS node
fins.dest_node_add = 20

# Ubuntu FINS node
fins.srce_node_add = 18

fins.connect(PLC_IP)

print("FINS communication OK")

