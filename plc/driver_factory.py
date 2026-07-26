from config.config_manager import ConfigManager
from plc.fins_driver import FINSDriver
from plc.simulator_driver import SimulatorDriver


def create_driver():
    cfg = ConfigManager()

    if cfg.driver == "simulator":
        return SimulatorDriver()

    if cfg.driver == "fins":
        return FINSDriver(
            ip=cfg.fins_ip,
            port=cfg.fins_port,
            plc_node=cfg.plc_node,
            pc_node=cfg.pc_node,
        )

    raise ValueError(
        f"Unsupported driver: {cfg.driver}"
    )
