from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.plc_tag_read_status import PlcTagReadStatus
from config.configuration_manager import ConfigurationManager
from config.environment import ENVIRONMENT_LABELS, ENVIRONMENTS, get_active_environment, set_active_environment
from config.plc_connection_manager import PROTOCOL_FIELDS, PROTOCOLS, PLCConnectionManager
from ui import auth
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH, get_equipment_list

# Depth/count caps for the OPC UA "Browse" picker below - bounds how
# long browsing a very large server's address space can take, since
# an unbounded walk could hang the page. Not a hard limit on what's
# usable as an address (the manual text box always still works) - if
# the node you need isn't found, it's most likely past these caps or
# outside the "Objects" branch these start browsing from.
OPCUA_BROWSE_MAX_DEPTH = 6
OPCUA_BROWSE_MAX_NODES = 500

# Which OPC UA VariantType names (asyncua's ua.VariantType.<X>.name)
# are compatible with each of SmartMachineAI's own tag data types -
# used by the OPC UA browse picker to block picking a node whose type
# doesn't actually match what the tag is configured as, rather than
# letting that surface later as a confusing read/write error.
OPCUA_COMPATIBLE_VARIANT_TYPES = {
    "REAL": {"Float", "Double"},
    "INT": {"SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"},
    "BOOL": {"Boolean"},
    "STRING": {"String"},
}


auth.require_role("admin")

current_user = auth.current_user()

st.title("🔌 PLC Connectivity")
st.caption(
    "Connect to a real PLC (Omron FINS, Modbus TCP, or OPC UA) and map its tag "
    "addresses onto the tags already configured in this system. Admin-only page."
)
st.info(
    "**Not tested against real hardware in this environment** - no PLC exists to "
    "connect to during development. The protocol drivers (`plc/modbus_driver.py`, "
    "`plc/opcua_driver.py`) are implemented against the well-documented address "
    "conventions of pymodbus/asyncua and verified with mocked responses, but the "
    "\"Test Connection\" button below is the real, honest way to confirm one "
    "actually talks to your hardware once you have it. Treat this page as a "
    "working starting point to refine together, not a finished/certified integration."
)

st.divider()
st.subheader("Environment")

stored_environment = get_active_environment()
running_process_environment = (
    "actual" if "actual" in str(CONFIG_DATABASE_PATH) else "simulation"
)

env_col, info_col = st.columns([2, 3])

with env_col:
    selected_environment = st.radio(
        "Active environment",
        list(ENVIRONMENTS),
        format_func=lambda e: ENVIRONMENT_LABELS[e],
        index=list(ENVIRONMENTS).index(stored_environment),
        key="environment_selector",
    )

with info_col:
    st.caption(
        "**Simulation** - the demo/presentation dataset (this one), safe to keep "
        "forever. **Actual** - a separate database for a real deployment, intended "
        "to start empty: no pre-built tags/equipment ship with it, configure them "
        "from scratch on the Equipment & Tag Configuration page, then map real PLC "
        "addresses below. If earlier development testing has left placeholder data "
        "here, clear it before going live - see `docs/REAL_PLC_CUTOVER_PROCEDURE.md`."
    )

if selected_environment != stored_environment:
    if st.button(f"Switch to {ENVIRONMENT_LABELS[selected_environment]}", type="primary"):
        set_active_environment(selected_environment)
        ConfigurationManager(database_path=CONFIG_DATABASE_PATH).write_audit_log(
            username=current_user["username"],
            action="switch_environment",
            entity_type="environment",
            entity_name=selected_environment,
            old_value=stored_environment,
            new_value=selected_environment,
        )
        st.success(
            f"Switched to {ENVIRONMENT_LABELS[selected_environment]}. This takes effect on "
            "restart, same as every other change on this page - restart plc_logger, "
            "event_monitor, and streamlit to pick it up."
        )
        st.rerun()

if running_process_environment != stored_environment:
    st.warning(
        f"This running app process is still using **{ENVIRONMENT_LABELS[running_process_environment]}** "
        f"data (marker file says {ENVIRONMENT_LABELS[stored_environment]}) - restart streamlit to "
        "match. This is expected right after a switch, not an error."
    )


@st.cache_resource
def _get_connection_manager() -> PLCConnectionManager:
    return PLCConnectionManager(database_path=CONFIG_DATABASE_PATH)


@st.cache_resource
def _get_config_manager() -> ConfigurationManager:
    return ConfigurationManager(database_path=CONFIG_DATABASE_PATH)


connection_manager = _get_connection_manager()
config_manager = _get_config_manager()

PROTOCOL_LABELS = {
    "simulator": "Simulator (no real hardware)",
    "fins": "Omron FINS",
    "modbus_tcp": "Modbus TCP",
    "opcua": "OPC UA",
    "s7": "Siemens S7",
}

FIELD_LABELS = {
    "host": "Host / IP address",
    "port": "Port",
    "plc_node": "PLC node",
    "pc_node": "PC node",
    "unit_id": "Unit / Slave ID",
    "endpoint_url": "Endpoint URL",
    "username": "Username (optional)",
    "password": "Password (optional)",
    "rack": "Rack",
    "slot": "Slot",
}

DEFAULT_PORTS = {"fins": 9600, "modbus_tcp": 502, "s7": 102}

# "simulator" is a real, valid protocol (Simulation mode uses it), but
# this dialog only ever renders in Actual mode (see the Connections
# section gate below) - offering it here would let someone create a
# pointless "simulator" connection while managing real hardware.
CONNECTABLE_PROTOCOLS = tuple(p for p in PROTOCOLS if p != "simulator")


def _test_connection(connection_row: dict) -> tuple[bool, str]:
    from plc.driver_factory import _create_from_connection

    def _attempt():
        driver = _create_from_connection(connection_row)
        driver.connect()
        driver.disconnect()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_attempt)
            future.result(timeout=8)
        return True, "Connected successfully."
    except FutureTimeoutError:
        return False, "Timed out after 8 seconds - check host/port and that the device is reachable."
    except Exception as error:
        return False, f"{type(error).__name__}: {error}"


def _browse_opcua_server(connection_row: dict) -> tuple[list[tuple[str, str]] | None, str]:
    """
    Connect to the active OPC UA server and walk its address space -
    returns (nodes, error_message); nodes is None on failure. Backs
    the per-tag "Browse" button below the manual address box, so a
    real server's own node tree can be searched instead of requiring
    the exact NodeId to be typed in from documentation.
    """
    from plc.driver_factory import _create_from_connection

    def _attempt():
        driver = _create_from_connection(connection_row)
        driver.connect()

        try:
            return driver.browse_nodes(
                max_depth=OPCUA_BROWSE_MAX_DEPTH,
                max_nodes=OPCUA_BROWSE_MAX_NODES,
            )
        finally:
            driver.disconnect()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_attempt)
            nodes = future.result(timeout=20)
        return nodes, ""
    except FutureTimeoutError:
        return None, "Timed out after 20 seconds browsing the server."
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


@st.dialog("Browse OPC UA Server")
def _opcua_browse_dialog(tag_name: str, tag_data_type: str, connection_row: dict) -> None:
    st.caption(f"Selecting an address for **{tag_name}** (configured as **{tag_data_type}**)")

    cache_key = f"opcua_browse_nodes_{connection_row['id']}"

    if cache_key not in st.session_state:
        with st.spinner("Connecting and browsing the server's address space..."):
            nodes, error = _browse_opcua_server(connection_row)
        if error:
            st.error(f"Could not browse the server: {error}")
            st.caption("You can still type the address manually and close this dialog.")
            return
        st.session_state[cache_key] = nodes

    nodes = st.session_state[cache_key]

    if not nodes:
        st.warning(
            "No readable/writable (Variable) nodes were found under Objects - "
            "either the server's address space is organized differently, or "
            "browsing hit its depth/count limit before finding any."
        )
        return

    if len(nodes) >= OPCUA_BROWSE_MAX_NODES:
        st.caption(
            f"Showing the first {OPCUA_BROWSE_MAX_NODES} nodes found - there may "
            "be more the search below doesn't cover."
        )

    search = st.text_input("Search", placeholder="Filter by name, e.g. \"pressure\"")

    filtered = [
        (path, node_id, data_type)
        for path, node_id, data_type in nodes
        if search.strip().lower() in path.lower()
    ] if search.strip() else nodes

    if not filtered:
        st.caption("No nodes match that search.")
        return

    compatible_types = OPCUA_COMPATIBLE_VARIANT_TYPES.get(tag_data_type, set())

    # Every node is shown regardless of type (so it's always visible
    # what the server actually offers), but picking one with a
    # confirmed-wrong type is blocked below rather than silently
    # allowed - the type mismatch would only surface much later, as a
    # confusing read/write error once the mapping is already saved.
    options = [
        f"{'✅' if data_type in compatible_types else '⚠️' if data_type != 'Unknown' else '❔'} "
        f"{path}   [{data_type}]   ({node_id})"
        for path, node_id, data_type in filtered
    ]
    choice_index = st.selectbox(
        f"{len(filtered)} node(s)", range(len(options)), format_func=lambda i: options[i]
    )

    selected_path, selected_node_id, selected_type = filtered[choice_index]
    type_matches = selected_type in compatible_types

    if not type_matches and selected_type != "Unknown":
        st.error(
            f"This node is **{selected_type}**, which doesn't match this tag's "
            f"configured data type (**{tag_data_type}**) - picking it would save an "
            "address that can't actually be read/written correctly. Choose a "
            "different node, or change the tag's data type on the Equipment & Tag "
            "Configuration page first if this is really the right one."
        )
    elif selected_type == "Unknown":
        st.warning(
            "Couldn't determine this node's data type from the server - "
            "you can still use it, but double-check it's really compatible "
            f"with **{tag_data_type}** before relying on it."
        )

    col_use, col_refresh = st.columns(2)

    with col_use:
        if st.button("Use this node", type="primary", disabled=not type_matches and selected_type != "Unknown"):
            st.session_state[f"addr_{tag_name}"] = selected_node_id
            st.rerun()

    with col_refresh:
        if st.button("Refresh (re-browse server)"):
            del st.session_state[cache_key]
            st.rerun()


@st.dialog("New PLC Connection")
def _new_connection_dialog() -> None:
    name = st.text_input("Connection name", placeholder="e.g. Main Factory PLC")
    protocol = st.selectbox("Protocol", CONNECTABLE_PROTOCOLS, format_func=lambda p: PROTOCOL_LABELS[p])

    fields = PROTOCOL_FIELDS[protocol]
    values: dict = {}

    for field in fields:
        label = FIELD_LABELS[field]

        if field == "port":
            values[field] = st.number_input(
                label, min_value=1, max_value=65535, value=DEFAULT_PORTS.get(protocol, 502), step=1
            )
        elif field in ("plc_node", "pc_node", "unit_id", "rack", "slot"):
            values[field] = st.number_input(label, min_value=0, max_value=255, value=0, step=1)
        elif field == "password":
            values[field] = st.text_input(label, type="password")
        else:
            values[field] = st.text_input(label)

    if st.button("Create", type="primary"):
        if not name.strip():
            st.error("Connection name is required.")
        elif protocol != "simulator" and any(
            field in ("host", "endpoint_url") and not str(values.get(field, "")).strip()
            for field in fields
        ):
            st.error("Host/endpoint is required for this protocol.")
        else:
            connection_manager.create_connection(
                name=name, protocol=protocol, created_by=current_user["username"], **values
            )
            st.success(f"Created connection '{name}'.")
            st.rerun()


st.subheader("Connections")

if stored_environment == "simulation":
    st.caption(
        "No connections apply in Simulation mode - data comes from the built-in "
        "simulator automatically. Switch to Actual above to configure a real PLC connection."
    )
else:
    if st.button("+ New Connection", type="primary"):
        _new_connection_dialog()

    connections = connection_manager.list_connections()

    if not connections:
        st.caption(
            "No connections configured yet - the system is currently using "
            "`config/settings.ini`'s legacy simulator/FINS settings."
        )
    else:
        connections_df = pd.DataFrame(
            [
                {
                    "Name": c["name"],
                    "Protocol": PROTOCOL_LABELS.get(c["protocol"], c["protocol"]),
                    "Host / Endpoint": c["host"] or c["endpoint_url"] or "-",
                    "Active": "✅" if c["is_active"] else "",
                }
                for c in connections
            ]
        )

        selection = st.dataframe(
            connections_df,
            hide_index=True,
            width="stretch",
            height=250,
            on_select="rerun",
            selection_mode="single-row",
            key="connections_table",
        )

        selected_positions = selection.selection.rows if selection else []

        if selected_positions:
            selected_connection = connections[selected_positions[0]]

            st.write(f"Selected: **{selected_connection['name']}** ({PROTOCOL_LABELS[selected_connection['protocol']]})")

            activate_col, test_col, delete_col = st.columns(3)

            with activate_col:
                if selected_connection["is_active"]:
                    if st.button("🔌 Disconnect"):
                        connection_manager.deactivate(
                            selected_connection["id"], changed_by=current_user["username"]
                        )
                        st.success(
                            f"'{selected_connection['name']}' is no longer active - no connection "
                            "is active now. Restart plc_logger to apply."
                        )
                        st.rerun()
                elif st.button("Set Active"):
                    connection_manager.set_active(selected_connection["id"], changed_by=current_user["username"])
                    st.success(f"'{selected_connection['name']}' is now active. Restart plc_logger to apply.")
                    st.rerun()

            with test_col:
                if st.button("Test Connection"):
                    with st.spinner("Connecting..."):
                        ok, message = _test_connection(selected_connection)
                    (st.success if ok else st.error)(message)

            with delete_col:
                if st.button("🗑 Delete"):
                    connection_manager.delete_connection(selected_connection["id"], changed_by=current_user["username"])
                    st.success(f"Deleted '{selected_connection['name']}'.")
                    st.rerun()
        else:
            st.caption("Select a row above to activate, test, or delete it.")

st.divider()
st.subheader("Tag Mapping")

if stored_environment == "simulation":
    st.caption("No tag mapping needed in Simulation mode - the simulator drives values directly.")
    active_protocol = None
else:
    active_connection = connection_manager.get_active()

    if active_connection is None:
        st.caption(
            "No connection is active - activate one above to map its real tag "
            "addresses. Currently falling back to `config/settings.ini`."
        )
        active_protocol = None
    else:
        active_protocol = active_connection["protocol"]
        st.caption(
            f"Mapping addresses for the active connection: **{active_connection['name']}** "
            f"({PROTOCOL_LABELS[active_protocol]})"
        )

if active_protocol and active_protocol != "simulator":
    equipment_list = get_equipment_list()
    equipment_options = {eq["display_name"]: eq for eq in equipment_list}
    selected_equipment_name = st.selectbox("Equipment", sorted(equipment_options), key="mapping_equipment")
    selected_equipment = equipment_options[selected_equipment_name]

    tags = config_manager.get_tags(equipment_name=selected_equipment["name"])
    existing_addresses = {
        row["tag_name"]: row
        for row in config_manager.get_tag_addresses(driver=active_protocol)
    }

    if not tags:
        st.caption("No tags configured for this equipment.")
    elif active_protocol == "opcua":
        # Not wrapped in st.form like the branch below - each row's
        # "Browse" button needs to trigger an st.dialog immediately,
        # which a plain st.button can do but a form-batched one can't
        # (only the form's own submit button reacts right away inside
        # a form). Each text_input's own `key` already persists its
        # value across reruns on its own, so a manual "Save mapping"
        # button reading straight from session_state stands in for
        # the form's batching instead.
        st.caption(
            "Address format: raw NodeId, e.g. `ns=2;i=1001` - or click "
            "Browse to pick one from the live server instead."
        )

        for tag in tags:
            tag_name = tag["tag_name"]
            address_key = f"addr_{tag_name}"

            if address_key not in st.session_state:
                existing = existing_addresses.get(tag_name)
                st.session_state[address_key] = existing["address"] if existing else ""

            address_col, browse_col = st.columns([4, 1])

            with address_col:
                st.text_input(f"{tag_name} ({tag['data_type']})", key=address_key)

            with browse_col:
                st.write("")  # spacer so the button lines up with the input, not its label
                if st.button("🔍 Browse", key=f"browse_btn_{tag_name}"):
                    _opcua_browse_dialog(tag_name, tag["data_type"], active_connection)

        if st.button(
            "Save mapping", type="primary", key=f"save_opcua_mapping_{selected_equipment['id']}"
        ):
            saved = 0
            for tag in tags:
                address = st.session_state.get(f"addr_{tag['tag_name']}", "").strip()
                if not address:
                    continue
                config_manager.set_tag_address(
                    tag_name=tag["tag_name"],
                    driver=active_protocol,
                    address=address,
                    enabled=True,
                    username=current_user["username"],
                )
                saved += 1
            st.success(f"Saved {saved} address mapping(s) for {selected_equipment['display_name']}.")
            st.rerun()
    else:
        st.caption(
            "Address format - FINS: `DM100`, `CIO0.01` | Modbus TCP: `HR:100`, `COIL:5`"
        )

        pending_key = f"pending_mapping_{selected_equipment['id']}"

        with st.form(f"mapping_form_{selected_equipment['id']}"):
            new_addresses: dict[str, str] = {}

            for tag in tags:
                existing = existing_addresses.get(tag["tag_name"])
                new_addresses[tag["tag_name"]] = st.text_input(
                    f"{tag['tag_name']} ({tag['data_type']})",
                    value=existing["address"] if existing else "",
                    key=f"addr_{tag['tag_name']}",
                )

            submitted = st.form_submit_button("Save mapping", type="primary")

        if submitted:
            saved = 0
            for tag_name, address in new_addresses.items():
                if not address.strip():
                    continue
                config_manager.set_tag_address(
                    tag_name=tag_name,
                    driver=active_protocol,
                    address=address.strip(),
                    enabled=True,
                    username=current_user["username"],
                )
                saved += 1
            st.success(f"Saved {saved} address mapping(s) for {selected_equipment['display_name']}.")
            st.rerun()
elif active_protocol == "simulator":
    st.caption("The active connection is the simulator - no real addresses to map.")

st.divider()
st.subheader("Per-Tag Read Health")

if stored_environment == "simulation":
    st.caption(
        "Not applicable in Simulation mode - the built-in simulator doesn't have "
        "per-tag read failures the way a real PLC connection can."
    )
else:
    st.caption(
        "Which specific tags have failed a read attempt, separate from overall "
        "Data Health (telemetry freshness) - useful for commissioning a new "
        "connection, since this can tell a whole-connection outage apart from "
        "one tag's address being wrong while everything else reads fine. Only "
        "ever lists tags that have failed at least once; a tag never shown here "
        "has never failed a read."
    )

    @st.cache_resource
    def _get_tag_read_status() -> PlcTagReadStatus:
        return PlcTagReadStatus(database_path=MACHINE_DATABASE_PATH)

    tag_read_status = _get_tag_read_status()
    read_status_rows = tag_read_status.get_all()

    if not read_status_rows:
        st.caption("No tag read failures recorded yet.")
    else:
        currently_failing = sum(1 for row in read_status_rows if row["consecutive_failures"] > 0)
        st.caption(
            f"{currently_failing} tag(s) currently failing, "
            f"{len(read_status_rows) - currently_failing} recovered from a past failure."
        )
        read_status_df = pd.DataFrame(
            [
                {
                    "Tag": row["tag_name"],
                    "Status": "🔴 Failing" if row["consecutive_failures"] > 0 else "🟢 Recovered",
                    "Consecutive failures": row["consecutive_failures"],
                    "Last success": row["last_success_at"] or "-",
                    "Last failure": row["last_failure_at"],
                }
                for row in read_status_rows
            ]
        )
        st.dataframe(read_status_df, hide_index=True, width="stretch")

st.divider()

with st.expander("Recent changes (audit log)"):
    audit_rows = config_manager.get_audit_log(limit=50)
    plc_audit_rows = [
        row for row in audit_rows if row["entity_type"] in ("plc_connection", "tag_address")
    ]

    if not plc_audit_rows:
        st.caption("No PLC connectivity changes recorded yet.")
    else:
        for row in plc_audit_rows:
            detail_suffix = f" ({row['details']})" if row["details"] else ""
            st.write(
                f"- {row['timestamp']} — **{row['action']}** `{row['entity_name']}`"
                f"{detail_suffix} (by {row['username']})"
            )
