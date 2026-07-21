import multiprocessing
import os
import subprocess
import sys


def build_executable():
    """使用 Nuitka 构建可执行文件"""

    # 设置环境变量
    os.environ['NUITKA_CC'] = 'msvc'
    os.environ['NUITKA_CXX'] = 'msvc'

    output_path = "output"
    exe_file_name = "downloader"
    cpu_count = multiprocessing.cpu_count()

    # --- 构建命令 ---
    cmd = [
        sys.executable, '-m', 'nuitka',
        '--standalone',
        '--python-flag=no_docstrings',
        '--onefile',
        f'--jobs={cpu_count}',
        f'--output-dir={output_path}',
        '--assume-yes-for-downloads',
        f'--output-filename={exe_file_name}',
        'src/downloader.py'
    ]

    try:
        # 执行构建命令
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        return False


def main():
    if build_executable():
        print("打包成功！")
    else:
        print("打包失败！")


if __name__ == "__main__":
    main()