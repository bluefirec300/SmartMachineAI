from config.config_manager import ConfigManager
from plc.simulator_driver import SimulatorDriver


def create_driver():
    """
    Picks a driver based on the active row in plc_connections
    (config.db) if one exists - this is what the PLC Connectivity
    admin page manages - falling back to the legacy
    config/settings.ini [DATA_SOURCE]/[FINS] values otherwise, so
    nothing changes for an existing simulator/FINS setup that predates
    the plc_connections table.

    Protocol-specific driver modules (fins/modbus/opcua) are imported
    lazily, inside their own branch, not at module top - so a missing
    optional dependency for a protocol nobody is using can never crash
    every caller of this factory (plc_logger, the tag-mapping UI,
    etc.) just by importing this file.
    """
    from config.plc_connection_manager import PLCConnectionManager

    active = PLCConnectionManager().get_active()

    if active is not None:
        return _create_from_connection(active)

    return _create_from_settings_ini()


def resolve_driver_name() -> str:
    """
    The driver/protocol name create_driver() actually resolves to
    ("opcua", "modbus_tcp", "fins", "s7", or "simulator"), without
    constructing the driver itself - the active connection's protocol
    if one's active, otherwise config/settings.ini's legacy driver.

    Exists because tag_addresses (and TagRegistry.get_enabled_for_
    driver()) key each tag's address by this exact string - a caller
    that builds its driver via create_driver() but queries tags using
    a DIFFERENT source for this name (e.g. config/settings.ini's
    driver directly, which never updates when a PLC connection is
    activated) will silently find zero tags for a real, correctly
    saved mapping. Found exactly this bug in app/plc_logger.py: it
    connected via the active OPC UA connection but queried
    ConfigManager().driver ("simulator", the untouched settings.ini
    default) for which tags to poll - always empty, regardless of what
    was actually mapped and saved on the Tag Mapping page.
    """
    from config.plc_connection_manager import PLCConnectionManager

    active = PLCConnectionManager().get_active()

    if active is not None:
        return active["protocol"]

    return ConfigManager().driver


def _create_from_connection(connection: dict):
    protocol = connection["protocol"]

    if protocol == "simulator":
        return SimulatorDriver()

    if protocol == "fins":
        from plc.fins_driver import FINSDriver

        return FINSDriver(
            ip=connection["host"],
            port=connection["port"] or 9600,
            plc_node=connection["plc_node"],
            pc_node=connection["pc_node"],
        )

    if protocol == "modbus_tcp":
        from plc.modbus_driver import ModbusDriver

        return ModbusDriver(
            host=connection["host"],
            port=connection["port"] or 502,
            unit_id=connection["unit_id"] or 1,
        )

    if protocol == "opcua":
        from plc.opcua_driver import OPCUADriver

        return OPCUADriver(
            endpoint_url=connection["endpoint_url"],
            username=connection["username"],
            password=connection["password"],
        )

    if protocol == "s7":
        from plc.s7_driver import S7Driver

        return S7Driver(
            ip=connection["host"],
            rack=connection["rack"] or 0,
            slot=connection["slot"] or 1,
            port=connection["port"] or 102,
        )

    raise ValueError(f"Unsupported protocol: {protocol}")


def _create_from_settings_ini():
    cfg = ConfigManager()

    if cfg.driver == "simulator":
        return SimulatorDriver()

    if cfg.driver == "fins":
        from plc.fins_driver import FINSDriver

        return FINSDriver(
            ip=cfg.fins_ip,
            port=cfg.fins_port,
            plc_node=cfg.plc_node,
            pc_node=cfg.pc_node,
        )

    raise ValueError(
        f"Unsupported driver: {cfg.driver}"
    )
