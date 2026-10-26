"""在当前 sane 环境构建可重复、无私人数据的 Windows 发布包。"""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_notices(target, internal):
    target.mkdir()
    versions = {}
    # 从实际使用的环境复制原始许可证，嵌套目录保持来源可追溯。
    packages = ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6",
                "rapidocr", "onnxruntime", "numpy", "opencv-python", "pillow",
                "omegaconf", "antlr4-python3-runtime", "loguru", "pyclipper",
                "shapely", "PyYAML", "requests", "urllib3", "charset-normalizer",
                "idna", "certifi", "Brotli", "tqdm", "six", "coloredlogs", "humanfriendly",
                "flatbuffers", "protobuf", "psutil", "packaging", "setuptools", "PyInstaller")
    for name in packages:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        versions[name] = distribution.version
        package_dir = target / name
        for file in distribution.files or []:
            label = str(file).lower()
            if any(part in label for part in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(file))
                if source.is_file() and "__pycache__" not in source.parts and \
                        source.suffix.lower() not in {".py", ".pyc", ".pyo", ".pyd", ".dll"}:
                    destination = package_dir / str(file).replace("../", "").replace("..\\", "")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    environment = Path(sys.executable).resolve().parent
    shutil.copy2(environment / "LICENSE_PYTHON.txt", target / "Python-LICENSE.txt")
    for source in (PROJECT / "packaging" / "licenses").iterdir():
        if source.is_file():
            shutil.copy2(source, target / source.name)
    # Python 链接的压缩/解析库与 SQLite 同样保留本地安装包提供的文本。
    documentation = environment / "Library" / "share" / "doc"
    for component in ("expat", "xz", "zlib"):
        directory = documentation / component
        if directory.is_dir():
            for file in directory.rglob("*"):
                if file.is_file() and any(word in file.name.lower() for word in ("copying", "license", "copyright")):
                    destination = target / component / file.relative_to(directory)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, destination)
    (target / "组件与版本.json").write_text(json.dumps({
        "Python": sys.version.split()[0], "OpenSSL": "3.5.7", **versions
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "说明.txt").write_text(
        "本包以动态库形式使用 Qt/PySide6/shiboken6，未修改这些第三方库。\n"
        "其 LGPL/GPL 许可证原文及其他组件原始许可随本目录提供。\n"
        "用户可为个人修改/调试目的替换 _internal 中对应第三方动态库，须保持 ABI 兼容。\n"
        "Qt/PySide6 对应版本源代码与构建说明：\n"
        "https://code.qt.io/cgit/pyside/pyside-setup.git/ （6.11.2 标签）\n"
        "https://code.qt.io/cgit/qt/qtbase.git/ （v6.11.2 标签）\n"
        "https://code.qt.io/cgit/qt/qtsvg.git/ （v6.11.2 标签）\n"
        "https://code.qt.io/cgit/qt/qtdeclarative.git/ （v6.11.2 标签）\n"
        "https://doc.qt.io/qtforpython-6/building_from_source/index.html\n"
        "RapidOCR 与 ONNX 模型来源：RapidAI/RapidOCR 3.9.2 的随包模型，"
        "PP-OCR 检测/识别与分类模型基于 PaddleOCR。\n"
        "https://github.com/RapidAI/RapidOCR\nhttps://github.com/PaddlePaddle/PaddleOCR\n"
        "OpenSSL 源码：https://github.com/openssl/openssl/tree/openssl-3.5.7\n"
        "其他组件按其各自原始许可分发；组件与版本.json 记录构建环境版本。\n",
        encoding="utf-8")


def audit_package(root):
    prohibited = {"clipboard.sqlite3", "api-settings.json", "shortcuts.json",
                  "data-locations.json", "last-error.txt", "instance.lock"}
    inventory = []
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(root)
        assert file.name not in prohibited, relative
        # OpenCV 自带 cv2/data 资源；禁止的是程序用户 data/，不能误判第三方资源。
        assert relative.parts[0] != "data", relative
        assert not any(part in {"tests", "work", "backups", "exports", "__pycache__",
                                ".agent-workbench"} for part in relative.parts), relative
        assert file.suffix.lower() not in {".sqlite3", ".sqlite", ".db", ".lnk"}, relative
        inventory.append({"path": relative.as_posix(), "bytes": file.stat().st_size, "sha256": sha256(file)})
    return inventory


def find_iscc():
    candidates = [os.environ.get("ISCC"), shutil.which("ISCC")]
    candidates += [str(Path(os.environ[variable]) / folder / "Inno Setup 6" / "ISCC.exe")
                   for variable, folder in (("LOCALAPPDATA", "Programs"), ("ProgramFiles(x86)", ""),
                                            ("ProgramFiles", "")) if os.environ.get(variable)]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("未找到 Inno Setup 6 的 ISCC.exe；可安装 Inno Setup，或用环境变量 ISCC 指定路径。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, default=PROJECT / "build")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assemble-only", action="store_true")
    parser.add_argument("--installer", action="store_true", help="另用 Inno Setup 生成一键安装包")
    parser.add_argument("--version", default="1.0.14")
    args = parser.parse_args()
    work, output = args.work_dir.resolve(), args.output_dir.resolve()
    if output.exists():
        raise SystemExit("输出目录已存在；请用新目录，避免覆盖用户运行产生的数据。")
    iscc = find_iscc() if args.installer else None
    if not args.assemble_only:
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                        "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
                        str(PROJECT / "packaging" / "clipboard-saver.spec")], check=True)
    source = work / "dist" / "剪贴收藏"
    assert (source / "剪贴收藏.exe").is_file()
    # 先审计构建目录，绝不把试运行产生的私人/测试数据复制成发布输入。
    audit_package(source)
    output.mkdir(parents=True)
    root = output / "剪贴收藏"
    shutil.copytree(source, root)
    # 模型直接取自构建环境的 rapidocr 包，不依赖本机 data/，干净克隆也能构建。
    sys.path.insert(0, str(PROJECT))
    from local_ocr import prepare_models
    prepare_models(root / "ocr-models")
    shutil.copy2(PROJECT / "packaging" / "使用说明.txt", root / "使用说明.txt")
    shutil.copy2(PROJECT / "LICENSE", root / "LICENSE.txt")
    collect_notices(root / "第三方许可", root / "_internal")
    inventory = audit_package(root)
    (output / "发布文件清单.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    # 发布附件用 ASCII 文件名：GitHub Release 会改写非 ASCII 的附件名。
    archive = output / f"ClipboardSaver-{args.version}-Windows-x64.zip"
    with ZipFile(archive, "x", ZIP_DEFLATED, compresslevel=9) as bundle:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                bundle.write(file, file.relative_to(output).as_posix())
    artifacts = [archive]
    if iscc:
        subprocess.run([iscc, "/Q", f"/DAppVersion={args.version}", f"/DSourceDir={root}",
                        f"/DOutputDir={output}", str(PROJECT / "packaging" / "installer" / "clipboard-saver.iss")],
                       check=True)
        artifacts.append(output / f"ClipboardSaver-{args.version}-Setup-x64.exe")
    (output / "SHA256.txt").write_text("".join(f"{sha256(file)}  {file.name}\n" for file in artifacts),
                                       encoding="utf-8")
    print(json.dumps({"artifacts": {file.name: file.stat().st_size for file in artifacts},
                      "files": len(inventory)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
