from enum import StrEnum


class StateFlags(StrEnum):
    #
    ready = "4"
    # 更新中
    updating = "6"
    # 未下载
    undownload = "514"
