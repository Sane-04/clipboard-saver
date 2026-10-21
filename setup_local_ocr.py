"""安装 requirements-ocr.txt 后，将包内模型准备到项目数据目录。"""
import importlib.util
import shutil
from pathlib import Path
from startup_runtime import prepare_runtime
prepare_runtime()
from data_locations import LocationStore


def main():
    package = importlib.util.find_spec("rapidocr")
    if package is None:
        raise SystemExit("请先在 sane 环境安装 requirements-ocr.txt。")
    source = Path(package.origin).parent / "models"
    target = LocationStore(Path(__file__).resolve().parent).locations.models
    names = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx",
             "ch_ppocr_mobile_v2.0_cls_mobile.onnx")
    if not all((source / name).is_file() for name in names):
        raise SystemExit("安装包的模型与当前配置不一致，请使用 requirements-ocr.txt 中的实测版本。")
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        if not (target / name).exists():
            shutil.copy2(source / name, target / name)
    print("本地 OCR 模型已准备完成。")


if __name__ == "__main__":
    main()
