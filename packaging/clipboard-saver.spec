# 构建仅收集程序和必要资源；个人 data/ 与路径配置不属于发布输入。
import os
import sys
from pathlib import Path

project = Path(SPECPATH).parent
environment = Path(sys.executable).resolve().parent
# 构建阶段同样隔离 PATH，避免将宿主 Codex/Poppler 的 ICU 和 VC 库带进成品。
system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
os.environ["PATH"] = os.pathsep.join(map(str, (
    environment, environment / "Library" / "bin", environment / "DLLs",
    environment / "Lib" / "site-packages" / "PySide6",
    environment / "Lib" / "site-packages" / "shiboken6",
    system / "System32", system)))

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

datas = [(str(project / "assets"), "assets")]
datas += collect_data_files("rapidocr", includes=["*.yaml", "**/*.txt", "**/*.yaml"])
datas += copy_metadata("rapidocr") + copy_metadata("onnxruntime")
binaries = collect_dynamic_libs("onnxruntime")

a = Analysis(
    [str(project / "app.py")],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["rapidocr.main", "rapidocr.inference_engine.onnxruntime",
                   "onnxruntime.capi.onnxruntime_pybind11_state", "release_check"],
    hookspath=[],
    runtime_hooks=[str(project / "packaging" / "runtime_hook.py")],
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "torch", "torchvision",
              "openvino", "paddle", "paddleocr", "IPython", "jupyter", "pytest",
              "PyQt5", "PyQt6", "PySide2", "onnxruntime.tools", "onnxruntime.quantization",
              "onnxruntime.transformers", "sympy",
              # 本应用使用 Qt/stdlib SSL，RapidOCR 接收数组；不收集这些可选后端。
              "OpenSSL", "cryptography", "bcrypt", "PIL.AvifImagePlugin", "PIL._avif"],
    noarchive=False,
)
# 自动依赖分析可能选择 PATH 中同名 SSL；明确替换成同一 sane 安装中的一对。
ssl_names = {"libcrypto-3-x64.dll", "libssl-3-x64.dll"}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in ssl_names]
for name in sorted(ssl_names):
    a.binaries.append(("Library/bin/" + name, str(environment / "Library" / "bin" / name), "BINARY"))
for destination, source, kind in a.binaries:
    path = Path(source).resolve()
    if not path.is_relative_to(environment) and not path.is_relative_to(system):
        raise RuntimeError("构建解析到了环境外的运行库：" + str(path))
# 此工具只用 QWidget 与静态图像；去除 Qt 自动扫描带入的 PDF、虚拟键盘及其 QML 依赖。
unused_qt = {"qpdf.dll", "qt6pdf.dll", "qtvirtualkeyboardplugin.dll", "qt6virtualkeyboard.dll",
             "qt6qml.dll", "qt6qmlmeta.dll", "qt6qmlmodels.dll", "qt6qmlworkerscript.dll", "qt6quick.dll"}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in unused_qt]
# 本地 OCR 只用静态图像处理，不使用 OpenCV 的视频读写/FFmpeg 插件。
# 保留 Qt 软件绘制、OpenCV 核心及所有原生 SSL 运行库，避免以兼容性换体积。
a.binaries = [entry for entry in a.binaries
              if not Path(entry[0]).name.lower().startswith("opencv_videoio_ffmpeg")]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="剪贴收藏",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, icon=str(project / "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="剪贴收藏")
