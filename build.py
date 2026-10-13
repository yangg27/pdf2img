# 构建脚本：把 PDF 转图片工具 打包成 Windows exe
#
# 用法：
#     pip install -r requirements.txt pyinstaller
#     python build.py
#
# 产物：dist/PDF转图片工具/  （one-dir 模式）
#
# 为什么用 one-dir 而不是 onefile：
#     tkinter 会拖入约 1000 个 tcl/tk 小文件，onefile 每次启动需逐个解压。
#     实测 onefile 启动约 47 秒，one-dir 仅约 1.5 秒（快约 31 倍）。
#     如果你确实需要单文件，把下面的 --onedir 改成 --onefile 即可，
#     但请知晓启动会显著变慢。

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "PDF转图片工具"
ENTRY = "pdf2img_gui.py"
ICON = os.path.join("assets", "icon.ico")

USE_ONEFILE = "--onefile" in sys.argv


def main():
    if not os.path.isfile(os.path.join(HERE, ENTRY)):
        sys.exit("找不到入口文件：%s" % ENTRY)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if USE_ONEFILE else "--onedir",
        "--windowed",
        "--name", APP_NAME,
        "--add-data", "%s%s." % (ICON, os.pathsep),
        ENTRY,
    ]
    if os.path.isfile(os.path.join(HERE, ICON)):
        cmd[cmd.index("--name"):cmd.index("--name")] = ["--icon", ICON]

    print("执行：", " ".join(cmd))
    subprocess.check_call(cmd, cwd=HERE)

    out = os.path.join(HERE, "dist", APP_NAME)
    if os.path.isdir(out):
        size = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, fs in os.walk(out) for f in fs
        )
        print("\n构建完成 → %s  (%.1f MB)" % (out, size / 1048576.0))
        print("双击运行：%s" % os.path.join(out, APP_NAME + ".exe"))
    else:
        exe = os.path.join(HERE, "dist", APP_NAME + ".exe")
        print("\n构建完成 → %s" % exe)


if __name__ == "__main__":
    main()