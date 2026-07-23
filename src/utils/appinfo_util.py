import struct
from io import BytesIO
from os import path

from cachetools.func import lru_cache
from loguru import logger
from vdf import binary_load

from utils.util import get_steam_install_path, get_steam_language


def parse_appinfo(fp):
    """
    读取并解析 Steam appinfo.vdf 二进制文件

    支持版本 39/40/41 三种 magic 格式:
      - v39: 'DV\\x07 (0x07564427)
      - v40: (DV\\x07 (0x07564428), 增加 Binary SHA1
      - v41: )DV\\x07 (0x07564429), 增加字符串表 + compact binary VDF

    :param fp: 以二进制模式打开的文件对象
    :return: (header, apps_iterator)
    """
    magic = fp.read(4)
    if magic not in (b"'DV\x07", b"(DV\x07", b")DV\x07"):
        raise SyntaxError(f"无效的 magic 头: {repr(magic)}")

    universe = struct.unpack('<I', fp.read(4))[0]
    # logger.info(f"magic={repr(magic)}, universe={universe}")

    if magic == b")DV\x07":
        return _parse_v41(fp, magic, universe)
    else:
        return _parse_v39_v40(fp, magic, universe)


# ======================== v39 / v40 格式 ========================

def _parse_v39_v40(fp, magic, universe):
    """解析 v39/v40 appinfo.vdf (per-app 条目, 标准 binary VDF)"""

    def _apps_iter():
        while True:
            appid = struct.unpack('<I', fp.read(4))[0]
            if appid == 0:
                break

            app = {
                'appid': appid,
                'size': struct.unpack('<I', fp.read(4))[0],
                'info_state': struct.unpack('<I', fp.read(4))[0],
                'last_updated': struct.unpack('<I', fp.read(4))[0],
                'access_token': struct.unpack('<Q', fp.read(8))[0],
                'sha1': fp.read(20),
                'change_number': struct.unpack('<I', fp.read(4))[0],
            }

            if magic == b"(DV\x07":  # v40
                app['data_sha1'] = fp.read(20)

            app['data'] = binary_load(fp)
            yield app

    return {'magic': magic, 'universe': universe}, _apps_iter()


# ======================== v41 格式 ========================

def _read_null_terminated_string(fp):
    """读取以 null 结尾的 UTF-8 字符串"""
    result = bytearray()
    while True:
        b = fp.read(1)
        if not b or b == b'\x00':
            break
        result.extend(b)
    return result.decode('utf-8', errors='replace')


def _parse_string_table(fp):
    """解析 v41 字符串表, 返回字符串列表"""
    string_count = struct.unpack('<I', fp.read(4))[0]
    strings = []
    for _ in range(string_count):
        strings.append(_read_null_terminated_string(fp))
    # logger.info(f"字符串表加载完成: {len(strings)} 个字符串")
    return strings


def _parse_compact_binary_vdf(fp, string_table):
    """
    解析 compact binary VDF (使用 4 字节字符串 ID 作为 key)

    格式: [type_byte][key_id_uint32][value...]
    与标准 binary VDF 区别: key 是 4 字节 string table 索引, 而非 null 结尾字符串
    """
    valid_types = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x0A, 0x0B}
    root = {}
    stack = [root]

    def _read_key_id():
        kid = struct.unpack('<I', fp.read(4))[0]
        if kid < len(string_table):
            return string_table[kid]
        return str(kid)

    while stack:
        t = fp.read(1)
        if not t:
            break
        t = t[0]

        if t == 0x08:  # BIN_END
            stack.pop()
            continue

        if t == 0x0B:  # BIN_END_ALT
            stack.pop()
            continue

        if t not in valid_types:
            logger.warning(f"未知的 binary VDF 类型: {t:#04x} at offset {fp.tell() - 1}")
            break

        key = _read_key_id()
        current = stack[-1]

        if t == 0x00:  # BIN_NONE → 子对象
            child = {}
            current[key] = child
            stack.append(child)

        elif t == 0x01:  # BIN_STRING
            val = _read_null_terminated_string(fp)
            current[key] = val

        elif t in (0x02,):  # BIN_INT32
            current[key] = struct.unpack('<i', fp.read(4))[0]

        elif t == 0x03:  # BIN_FLOAT32
            current[key] = struct.unpack('<f', fp.read(4))[0]

        elif t in (0x04, 0x06):  # BIN_POINTER / BIN_COLOR
            current[key] = struct.unpack('<I', fp.read(4))[0]

        elif t == 0x05:  # BIN_WIDESTRING
            val = bytearray()
            while True:
                b = fp.read(2)
                if b in (b'\x00\x00', b''):
                    break
                val.extend(b[:1])
            current[key] = val.decode('utf-16-le', errors='replace')

        elif t == 0x07:  # BIN_UINT64
            current[key] = struct.unpack('<Q', fp.read(8))[0]

        elif t == 0x0A:  # BIN_INT64
            current[key] = struct.unpack('<q', fp.read(8))[0]

    return root


def _parse_v41(fp, magic, universe):
    """
    解析 v41 appinfo.vdf

    格式:
      - offset  0: magic (4 bytes)
      - offset  4: universe (4 bytes)
      - offset  8: string_table_offset (8 bytes, int64)
      - offset 16: app entries 开始 (compact binary VDF, 使用字符串表)
    """
    string_table_offset = struct.unpack('<q', fp.read(8))[0]
    # logger.info(f"字符串表偏移: {string_table_offset}")

    # 保存当前读取位置, 跳转到字符串表读取
    entry_start_pos = fp.tell()
    fp.seek(string_table_offset)
    string_table = _parse_string_table(fp)
    fp.seek(entry_start_pos)

    def _apps_iter():
        while True:
            appid = struct.unpack('<I', fp.read(4))[0]
            if appid == 0:
                break

            size = struct.unpack('<I', fp.read(4))[0]
            # size 从当前 position 开始计算 (position 在读取 appid + size 之后)
            end_pos = fp.tell() + size

            app = {
                'appid': appid,
                'size': size,
                'info_state': struct.unpack('<I', fp.read(4))[0],
                'last_updated': struct.unpack('<I', fp.read(4))[0],
                'access_token': struct.unpack('<Q', fp.read(8))[0],
                'sha1': fp.read(20),
                'change_number': struct.unpack('<I', fp.read(4))[0],
                'data_sha1': fp.read(20),
            }

            # binary VDF 读取到 end_pos
            vdf_start = fp.tell()
            vdf_size = end_pos - vdf_start
            if vdf_size > 0:
                vdf_raw = fp.read(vdf_size)
                app['data'] = _parse_compact_binary_vdf(
                    BytesIO(vdf_raw), string_table
                )
            else:
                app['data'] = {}

            yield app

    return {
        'magic': magic,
        'universe': universe,
        'string_table_size': len(string_table),
    }, _apps_iter()


@lru_cache(maxsize=1)
def get_game_localized_name_map() -> dict[str, str]:
    """获取游戏名称本地化映射"""
    try:
        language = get_steam_language()
        result = {}
        with open(path.join(get_steam_install_path(), "appcache", "appinfo.vdf"), 'rb') as f:
            header, apps = parse_appinfo(f)
            for app in apps:
                app_info = app.get("data", {}).get("appinfo", {})
                app_id = app_info.get("appid")
                if app_id:
                    result[str(app_id)] = app_info.get("common", {}).get("name_localized", {}).get(language, app_info.get("common", {}).get("name"))
        return result
    except Exception as _:
        pass
    return {}

# if __name__ == "__main__":
#     with open(path.join(get_steam_install_path(), "appcache", "appinfo.vdf"), 'rb') as f:
#         header, apps = parse_appinfo(f)
#         print(header)
#         count = 0
#         for app in apps:
#             app_info = app.get("data", {}).get("appinfo", {})
#             # print(app_info.get("common",{}).get("type"))
#             if app_info.get("appid") and app_info.get("common", {}).get("type") == "Game":
#                 print(app_info.get("appid"))
#                 print(app_info.get("common", {}).get("name"))
#                 print(app_info.get("common", {}).get("name_localized"))
#             # print(f"appid={app['appid']}, name={name}")
#             count += 1
#         print(count)
