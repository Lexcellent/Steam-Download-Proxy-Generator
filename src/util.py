import lzma
import random
import struct
import time
import winreg
from binascii import crc32
from io import BytesIO
from os import path
from pathlib import Path
from zipfile import ZipFile

import httpx
import requests
import vdf
import zstandard as zstd
from loguru import logger
from steam.client.cdn import ContentServer, get_content_servers_from_webapi
from steam.core.crypto import symmetric_decrypt

from entitys.Depot import InstalledDepot
from entitys.Game import InstalledGame
from enums.Status import StateFlags


def get_steam_install_path():
    """从注册表获取Steam安装路径"""
    # 打开注册表键
    key = winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\WOW6432Node\Valve\Steam"
    )
    # 查询 InstallPath 的值
    steam_path, _ = winreg.QueryValueEx(key, "InstallPath")
    winreg.CloseKey(key)
    return steam_path


def get_installed_games() -> list[InstalledGame]:
    """获取已安装完毕的游戏列表信息"""
    # 从注册表获取steam安装目录
    steam_path = get_steam_install_path()
    # 解析已安装了哪些游戏
    with open(path.join(path.join(steam_path, "steamapps"), "libraryfolders.vdf"), "r", encoding='utf-8') as f:
        library_folders = vdf.load(f)
    installed_games: list[InstalledGame] = list()
    for library_index in library_folders["libraryfolders"]:
        library_path = library_folders["libraryfolders"][library_index]["path"]
        steam_app_path = path.join(library_path, "steamapps")
        acf_list = list(Path(steam_app_path).glob('*.acf'))
        for acf_path in acf_list:
            with open(acf_path, "r", encoding='utf-8') as f:
                acf_json = vdf.load(f)
                if acf_json["AppState"]["StateFlags"] != StateFlags.ready:
                    continue
                installed_depots: list[InstalledDepot] = list()
                for depot_id in acf_json["AppState"]["InstalledDepots"]:
                    installed_depots.append(
                        InstalledDepot(
                            depot_id,
                            acf_json["AppState"]["InstalledDepots"][depot_id]["manifest"],
                            acf_json["AppState"]["InstalledDepots"][depot_id]["size"]
                        )
                    )
                installed_games.append(
                    InstalledGame(
                        acf_json["AppState"]["appid"],
                        acf_json["AppState"]["name"],
                        acf_json["AppState"]["installdir"],
                        path.join(steam_app_path, "common", acf_json["AppState"]["installdir"]),
                        acf_json["AppState"]["SizeOnDisk"],
                        installed_depots
                    )
                )
    installed_games.sort(key=lambda game: game.name)
    return installed_games


def get_depots_decryption_key(depot_ids: list[str]) -> dict[str, str]:
    """获取depots的解密密钥"""
    config_path = path.join(get_steam_install_path(), "config", "config.vdf")
    with open(config_path, "r", encoding='utf-8') as f:
        config = vdf.load(f)
    depots = config["InstallConfigStore"]["Software"]["Valve"]["Steam"]["depots"]
    result = {}
    for depot_id in depot_ids:
        result[depot_id] = depots[depot_id]["DecryptionKey"]
    return result


def decompress_chunk(data: bytes) -> bytes:
    """
    解压 Steam chunk 数据

    支持:
    VSZa -> ZSTD
    VZa  -> LZMA
    ZIP
    RAW
    """

    # 1. VSZa (ZSTD)
    if data[:4] == b'VSZa':
        return _decompress_vsza(data)

    # 2. VZa (LZMA)
    if data[:3] == b'VZa':
        return _decompress_vz(data)

    # 3. ZIP
    if data[:4] == b'PK\x03\x04':
        return _decompress_zip(data)

    # 4. raw
    return data


def _decompress_vsza(data: bytes) -> bytes:
    """
    Steam VSZa chunk 解压
    格式:

    VSZa
    ----
    zstd frame
    ----
    footer
    """
    if data[:4] != b'VSZa':
        raise ValueError("不是 VSZa")
    # 找 zstd magic
    zstd_magic = b'\x28\xb5\x2f\xfd'
    pos = data.find(zstd_magic)
    if pos < 0:
        raise ValueError("找不到 zstd frame")
    zstd_data = data[pos:]
    dctx = zstd.ZstdDecompressor()
    try:
        result = dctx.decompress(zstd_data)
    except Exception as e:
        raise ValueError(f"VSZa zstd解压失败: {e}")
    return result


def _decompress_vz(data: bytes) -> bytes:
    """解压 VZ 格式 - 基于你的代码逻辑"""
    # 验证格式
    if data[:2] != b'VZ':
        raise ValueError("不是 VZ 格式")
    if data[2:3] != b'a':
        raise ValueError("无效的 VZ 版本")
    if data[-2:] != b'zv':
        raise ValueError("无效的 VZ 尾部")

    # 提取 LZMA 参数 (使用你的方式)
    vzfilter = lzma._decode_filter_properties(lzma.FILTER_LZMA1, data[7:12])
    vzdec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[vzfilter])

    # 提取校验和与解压大小
    checksum, decompressed_size = struct.unpack('<II', data[-10:-2])

    # 解压 (跳过12字节头部，去掉9字节尾部)
    result = vzdec.decompress(data[12:-9])[:decompressed_size]

    # CRC 校验
    if crc32(result) != checksum:
        raise ValueError("CRC32 校验失败")

    return result


def _decompress_zip(data: bytes) -> bytes:
    """解压 ZIP 格式"""
    try:
        with ZipFile(BytesIO(data)) as zf:
            if zf.filelist:
                return zf.read(zf.filelist[0])
    except Exception as e:
        raise ValueError(f"ZIP 解压失败: {e}")

    return data


_httpx_client = None


def get_httpx_client() -> httpx.Client:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.Client()
    return _httpx_client


STEAM_DOWNLOAD_SERVERS = []


def download_and_decrypt_chunk(depot_id: str, chunk_sha_hex: str, decryption_key: str):
    """下载并解密数据块"""
    servers = STEAM_DOWNLOAD_SERVERS.copy()
    random.shuffle(servers)
    for server_host in servers:
        try:
            # 下载
            url = f"http://{server_host}/depot/{depot_id}/chunk/{chunk_sha_hex}"
            resp = get_httpx_client().get(url, timeout=10)
            if resp.status_code != 200:
                continue
            # 解密
            key = bytes.fromhex(decryption_key)
            data = symmetric_decrypt(resp.content, key)
            return decompress_chunk(data)
        except httpx.ReadTimeout as _:
            pass
        except httpx.ReadError as _:
            pass
        except Exception as e:
            logger.exception(e)
    raise ConnectionError("下载失败，检查网络状况")


def test_cdn_server(server: ContentServer, depot_id: str, chunk_sha: str):
    """
    测试 Steam CDN 节点下载 chunk 耗时
    """

    if not server.host.endswith(".steamcontent.com"):
        return None
    url = f"http://{server.host}/depot/{depot_id}/chunk/{chunk_sha}"
    try:
        start = time.perf_counter()
        resp = get_httpx_client().get(url, timeout=30)
        cost = time.perf_counter() - start
        if resp.status_code == 200 and cost < 5:
            return {
                "host": server.host,
                "time": cost,
                "size": len(resp.content)
            }
    except Exception:
        pass
    return None


def get_content_servers(cell_id: int, num_servers=40):
    for index in range(3):
        try:
            return get_content_servers_from_webapi(cell_id, num_servers=num_servers)
        except requests.exceptions.ConnectTimeout as _:
            logger.warning(f"cell_id:{cell_id} 获取超时，重试：{index + 1}")
        except requests.exceptions.ConnectionError as _:
            logger.warning(f"cell_id:{cell_id} 连接失败，重试：{index + 1}")
    return []


def refresh_cdn_server():
    logger.info("刷新服务器节点列表")
    servers = get_content_servers(0)
    results = []
    for server in servers:
        result = test_cdn_server(server, "3167021", "ccb1bf52956792fac3372220ced49880e0bcec2b")
        if result:
            results.append(result)
    servers = get_content_servers(33)
    for server in servers:
        result = test_cdn_server(server, "3167021", "ccb1bf52956792fac3372220ced49880e0bcec2b")
        if result:
            results.append(result)
    if len(results) == 0:
        raise Exception("未找到合适的下载节点,请检查网络情况是否能访问https://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/")
    results.sort(key=lambda x: x["time"])
    logger.debug(results)
    global STEAM_DOWNLOAD_SERVERS
    STEAM_DOWNLOAD_SERVERS = [result["host"] for result in results]
    logger.info(f"刷新完成，{len(STEAM_DOWNLOAD_SERVERS)}个节点：{STEAM_DOWNLOAD_SERVERS}")
