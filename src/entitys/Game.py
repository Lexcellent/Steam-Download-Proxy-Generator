from dataclasses import dataclass

from entitys.Depot import InstalledDepot


@dataclass
class InstalledGame:
    appid: str
    name: str
    install_dir: str
    install_abs_dir: str
    size_on_disk: str
    installed_depots: list[InstalledDepot]
