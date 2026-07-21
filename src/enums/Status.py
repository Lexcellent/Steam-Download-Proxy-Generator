from enum import StrEnum


class StateFlags(StrEnum):
    #
    ready = "4"
    # 更新中
    updating = "6"
    # 未下载
    undownload = "514"

class MappingFlags(StrEnum):
    file = "0"
    dir = "64"