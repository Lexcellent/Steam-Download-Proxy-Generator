import json
import shutil
from os import path
from pathlib import Path

import humanize
from loguru import logger

from config import OUTPUT_PATH
from utils.appinfo_util import get_game_localized_name_map
from utils.util import get_installed_games, get_steam_install_path, get_depots_decryption_key


def main():
    installed_games = get_installed_games()
    while True:
        print("选择要生成的游戏:")
        for index in range(len(installed_games)):
            print(f"{index + 1} - {get_game_localized_name_map().get(str(installed_games[index].appid), installed_games[index].name)} (appid:{installed_games[index].appid},size:{humanize.naturalsize(installed_games[index].size_on_disk)})")
        number = input("输入要生成的游戏序号:\n")
        if not number.isdigit():
            logger.warning("需要输入整数数字")
            continue
        if int(number) > len(installed_games):
            logger.warning("需要输入有效数字")
            continue
        selected_game = installed_games[int(number) - 1]
        logger.debug(f"开始处理【{selected_game.name}】文件……")
        # 目标文件夹
        target_dir = Path(path.join(OUTPUT_PATH, selected_game.install_dir))
        # 复制清单文件
        depot_cache_path = path.join(get_steam_install_path(), "depotcache")
        depot_ids = []
        for depot in selected_game.installed_depots:
            depot_ids.append(depot.depot_id)
            depot_file_name = f"{depot.depot_id}_{depot.mainfest_id}.manifest"
            depot_manifest_path = path.join(depot_cache_path, depot_file_name)
            if not path.exists(depot_manifest_path):
                raise FileNotFoundError(f"文件未找到:{depot_manifest_path}")
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(depot_manifest_path, target_dir)
        # 保存密钥文件
        decryption_keys = get_depots_decryption_key(depot_ids)
        with open(path.join(target_dir, "depots.json"), "w") as f:
            json.dump(decryption_keys, f)
        # 复制下载器到目标文件夹
        if path.exists("downloader.exe"):
            shutil.copy("downloader.exe", target_dir)
        logger.success(f"【{selected_game.name}】生成完毕")
        input("回车键继续……")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(e)
    finally:
        input("回车键退出...")
