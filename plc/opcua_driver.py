"""
OPC UA driver.

Address format: a raw OPC UA NodeId string, exactly as the server
exposes it - e.g. "ns=2;i=1001" or "ns=2;s=Machine.Compressor.Pressure".
Set per-tag in tag_addresses.address, same mapping mechanism already
used for the simulator/FINS/Modbus drivers.

Uses asyncua.sync.Client - a synchronous wrapper asyncua provides
specifically for exactly this kind of blocking, poll-driven use case
(the rest of this codebase's driver interface is synchronous - see
plc/base_driver.py - so this avoids introducing asyncio into
app/plc_logger.py's simple polling loop).
"""

from __future__ import annotations

from plc.base_driver import BaseDriver


class OPCUADriver(BaseDriver):
    def __init__(
        self,
        endpoint_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 4,
    ):
        self.endpoint_url = endpoint_url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.client = None

    def connect(self):
        from asyncua.sync import Client

        self.client = Client(url=self.endpoint_url, timeout=self.timeout)

        if self.username:
            self.client.set_user(self.username)
        if self.password:
            self.client.set_password(self.password)

        self.client.connect()

        print(f"OPC UA connected to {self.endpoint_url}")

    def disconnect(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.client = None
        print("OPC UA disconnected.")

    def read(self, tag):
        self._ensure_connected()

        name = tag["name"]
        address = tag["address"]

        node = self.client.get_node(address)
        value = node.read_value()

        return {"name": name, "address": address, "value": value}

    def read_all(self, tags):
        self._ensure_connected()

        values = []

        for tag in tags:
            try:
                values.append(self.read(tag))
            except Exception as error:
                print(
                    f"OPC UA read error for {tag.get('name', 'UnknownTag')} "
                    f"({tag.get('address', 'UnknownAddress')}): {error}"
                )

        return values

    def write(self, tag, value):
        self._ensure_connected()

        node = self.client.get_node(tag["address"])
        node.write_value(value)

    def browse_nodes(
        self,
        max_depth: int = 6,
        max_nodes: int = 500,
    ) -> list[tuple[str, str, str]]:
        """
        Walk the server's address space starting from Objects,
        returning (display_path, node_id_string, data_type_name)
        triples for every Variable node found - the only node class
        that can actually be read/written as a tag address (Object
        nodes are just organizational folders, traversed but never
        themselves returned). Backs the Tag Mapping page's "Browse"
        picker, so a real server's own address space can be searched
        instead of requiring the exact NodeId to be typed in by hand.
        data_type_name (e.g. "Boolean", "Int16", "Double") lets the
        picker show what each node actually is and block picking one
        that doesn't match the target tag's configured data type.

        Depth- and count-bounded so browsing a very large server can't
        hang the UI indefinitely - if a cap is hit, the returned list
        is simply incomplete, not an error; callers should treat it as
        "the first N found", not exhaustive, and narrow their search
        (or increase the caps) if what they need isn't there.

        Uses get_children_descriptions() (one Browse-service round
        trip per level, returning name/class/id together) rather than
        a separate read call per child per attribute - meaningfully
        fewer network round trips for a server with hundreds of nodes.
        Reading each Variable's data type is still one extra round
        trip per variable (no batched equivalent in the Browse
        service) - acceptable since the namespace-0 filter below
        already keeps the Variable count small in practice.

        Skips namespace 0 entirely (except the Objects root itself,
        which is always ns=0 and has to be the starting point) - by
        spec, namespace 0 is reserved for the OPC Foundation's own
        standard information model (ServerCapabilities,
        PublishSubscribe, diagnostics, ...), never actual vendor/user
        tags, which by spec always live in a non-zero namespace.
        Verified against a real running server, not just assumed from
        the spec: without this filter, a 3-tag test server returned
        232 "nodes" - 229 of them standard server-metadata boilerplate
        identical on every OPC UA server, burying the 3 real tags at
        the very end and eating most of the max_nodes budget for
        nothing. With it, only real tags are ever returned.
        """
        self._ensure_connected()

        from asyncua import ua

        results: list[tuple[str, str, str]] = []
        root = self.client.get_objects_node()
        self._browse_recursive(root, "Objects", 0, max_depth, max_nodes, results, ua)

        return results

    def _browse_recursive(self, node, path, depth, max_depth, max_nodes, results, ua):
        if depth > max_depth or len(results) >= max_nodes:
            return

        try:
            children = node.get_children_descriptions(
                nodeclassmask=ua.NodeClass.Object | ua.NodeClass.Variable
            )
        except Exception:
            return

        for child in children:
            if len(results) >= max_nodes:
                return

            if child.NodeId.NamespaceIndex == 0:
                # Standard OPC Foundation node (Server, PublishSubscribe,
                # diagnostics, ...) - never a real tag, and its own
                # children are the same, so skip both including it and
                # recursing into it.
                continue

            display_name = (
                child.DisplayName.Text or child.BrowseName.Name or ""
            ).strip()
            child_path = f"{path} > {display_name}" if display_name else path

            if child.NodeClass_ == ua.NodeClass.Variable:
                try:
                    child_node = self.client.get_node(child.NodeId)
                    data_type_name = child_node.read_data_type_as_variant_type().name
                except Exception:
                    data_type_name = "Unknown"

                results.append(
                    (child_path, child.NodeId.to_string(), data_type_name)
                )
            elif child.NodeClass_ == ua.NodeClass.Object:
                try:
                    child_node = self.client.get_node(child.NodeId)
                except Exception:
                    continue

                self._browse_recursive(
                    child_node, child_path, depth + 1, max_depth, max_nodes, results, ua
                )

    def _ensure_connected(self):
        if self.client is None:
            raise ConnectionError("OPC UA driver is not connected.")
