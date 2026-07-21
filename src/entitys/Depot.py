from dataclasses import dataclass


@dataclass
class InstalledDepot:
    depot_id: str
    mainfest_id: str
    size: str
