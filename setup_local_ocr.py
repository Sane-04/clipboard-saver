"""可选：提前把 rapidocr 包内模型准备到当前模型目录；首次识别时也会自动完成。"""
from pathlib import Path
from startup_runtime import prepare_runtime
prepare_runtime()
from data_locations import LocationStore
from local_ocr import prepare_models


def main():
    try:
        prepare_models(LocationStore(Path(__file__).resolve().parent).locations.models)
    except FileNotFoundError:
        raise SystemExit("未找到 rapidocr 自带模型，请先运行 pip install -r requirements.txt。")
    print("本地 OCR 模型已准备完成。")


if __name__ == "__main__":
    main()
