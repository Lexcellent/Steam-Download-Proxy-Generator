import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from os import path
from pathlib import Path

from loguru import logger
from steam.core.manifest import DepotManifest
from tqdm import tqdm

from util import download_and_decrypt_chunk


def main():
    # 读取depot文件
    with open("depots.json", "r") as f:
        depots = json.load(f)
    current_dir_path = Path(".")
    # 总文件大小
    total_size = 0

    manifests = []
    for depot_id in depots:
        manifest_file_name = list(current_dir_path.glob(f"{depot_id}*.manifest"))[0]
        # 解析manifest文件内容
        with open(manifest_file_name, "rb") as f:
            manifest = DepotManifest(f.read())
        total_size += int(manifest.metadata.cb_disk_original)
        manifests.append(manifest)

    pbar = tqdm(total=total_size, unit="B", unit_scale=True)
    file_lock = threading.Lock()
    for manifest in manifests:
        for mapping in manifest.payload.mappings:
            # print(mapping.filename)
            # print(mapping.sha_content.hex())
            # print(humanize.naturalsize(mapping.size))
            # print(mapping)
            # 如果文件已存在，对比文件大小
            if path.exists(mapping.filename) and path.getsize(mapping.filename) == mapping.size:
                # 已存在,直接更新进度
                pbar.update(int(mapping.size))
                continue
            # 创建文件（如果不存在）
            game_file = Path(mapping.filename)
            game_file.parent.mkdir(parents=True, exist_ok=True)
            game_file.touch(exist_ok=True)

            def download(chunk):
                # print(chunk)
                chunk_data = download_and_decrypt_chunk(str(manifest.depot_id), chunk.sha.hex(), depots[str(manifest.depot_id)])
                if len(chunk_data) != chunk.cb_original:
                    logger.warning(
                        f"Chunk 大小不匹配: {mapping.filename} offset={chunk.offset}, "
                        f"期望 {chunk.cb_original}, 实际 {len(chunk_data)}"
                    )
                # 按偏移量写入文件
                with file_lock:
                    with open(mapping.filename, 'r+b') as f:
                        f.seek(chunk.offset)
                        f.write(chunk_data)
                    pbar.update(int(chunk.cb_original))

            # 下载并解密数据块
            with ThreadPoolExecutor(max_workers=128) as executor:
                # 提交所有任务
                futures = [executor.submit(download, chunk) for chunk in sorted(mapping.chunks,key=lambda x: x.offset)]
                # 等待完成
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.exception(e)
    pbar.close()
    logger.success("下载完毕")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(e)
    finally:
        input("回车键退出...")
