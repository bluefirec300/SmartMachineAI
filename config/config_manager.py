from configparser import ConfigParser
from pathlib import Path


class ConfigManager:

    def __init__(self):

        self.project_root = Path(__file__).resolve().parent.parent

        self.config_path = (
            self.project_root
            / "config"
            / "settings.ini"
        )

        self.config = ConfigParser()

        self.config.read(self.config_path)

    @property
    def machine_name(self):
        return self.config["MACHINE"]["name"]

    @property
    def driver(self):
        return self.config["DATA_SOURCE"]["driver"].lower()

    @property
    def scan_interval(self):
        return self.config.getint(
            "DATA_SOURCE",
            "scan_interval"
        )

    @property
    def database_path(self):

        return (
            self.project_root
            / self.config["DATABASE"]["path"]
        )

    @property
    def fins_ip(self):
        return self.config["FINS"]["ip"]

    @property
    def fins_port(self):
        return self.config.getint(
            "FINS",
            "port"
        )

    @property
    def plc_node(self):
        return self.config.getint(
            "FINS",
            "plc_node"
        )

    @property
    def pc_node(self):
        return self.config.getint(
            "FINS",
            "pc_node"
        )

    @property
    def ai_provider(self):
        return self.config["AI"]["provider"]

    @property
    def ai_model(self):
        return self.config["AI"]["model"]

    @property
    def ollama_url(self):
        return self.config["OLLAMA"]["url"]

    @property
    def ollama_model(self):
        return self.config["OLLAMA"]["model"]
