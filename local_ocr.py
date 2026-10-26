"""常驻 CPU OCR 工作线程。只接收成品选区，不联网、不读取历史或剪贴板。"""
from startup_runtime import prepare_runtime
prepare_runtime()

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import perf_counter

import numpy as np
from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication
from app_paths import default_model_dir

MODEL_DIR = default_model_dir()
MODEL_FILES = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx",
               "ch_ppocr_mobile_v2.0_cls_mobile.onnx")


def prepare_models(model_dir):
    """模型缺失时从已安装的 rapidocr 包复制，源码运行无需额外准备步骤；不联网下载。"""
    import importlib.util
    import shutil
    model_dir = Path(model_dir)
    missing = [name for name in MODEL_FILES if not (model_dir / name).is_file()]
    if not missing:
        return
    package = importlib.util.find_spec("rapidocr")
    source = Path(package.origin).parent / "models" if package is not None else None
    if source is None or not all((source / name).is_file() for name in missing):
        raise FileNotFoundError("本地 OCR 模型缺失")
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in missing:
        shutil.copy2(source / name, model_dir / name)


def create_engine(model_dir=None):
    model_dir = Path(model_dir).resolve() if model_dir is not None else MODEL_DIR
    from rapidocr import RapidOCR
    prepare_models(model_dir)
    # 显式使用已准备好的模型，运行时不下载或上传图像。
    return RapidOCR(params={
        "Global.log_level": "error", "Global.model_root_dir": str(model_dir),
        "Det.model_path": str(model_dir / MODEL_FILES[0]),
        "Rec.model_path": str(model_dir / MODEL_FILES[1]),
        "Cls.model_path": str(model_dir / MODEL_FILES[2]),
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Det.limit_type": "max", "Det.limit_side_len": 1280,
    })


def image_to_bgr(image):
    # 将透明内容合成白底，防止识别不可见像素；不修改输入成品。
    flat = QImage(image.size(), QImage.Format.Format_RGB888)
    flat.fill(QColor("white"))
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    rows = np.frombuffer(flat.constBits(), dtype=np.uint8).reshape(flat.height(), flat.bytesPerLine())
    rgb = rows[:, :flat.width() * 3].reshape(flat.height(), flat.width(), 3)
    return rgb[:, :, ::-1].copy()


def output_to_data(output, width, height):
    boxes, texts, scores = output.boxes, output.txts, output.scores
    blocks = []
    if boxes is not None and texts is not None:
        for box, text, score in zip(boxes, texts, scores if scores is not None else [1] * len(texts)):
            # 多边形归一化为共同 OCR 契约，裁切偏移由编辑器映射回底图。
            polygon = np.asarray(box, dtype=float)
            if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
                raise ValueError("OCR 位置无效")
            left, top = np.maximum(0, np.min(polygon, axis=0))
            right, bottom = np.minimum([width, height], np.max(polygon, axis=0))
            if right <= left or bottom <= top or not str(text).strip():
                continue
            blocks.append({"id": str(len(blocks) + 1), "text": str(text),
                           "bbox": [float(left / width), float(top / height),
                                    float(right / width), float(bottom / height)],
                           "confidence": round(float(score), 4)})
    return {"blocks": blocks, "engine": "RapidOCR"}


def recognize_image(engine, image, cancelled):
    width, height = image.width(), image.height()
    if height <= 1280 or height < width * 2:
        return output_to_data(engine(image_to_bgr(image)), width, height)
    tile_height, overlap = 1280, 160
    step = tile_height-overlap
    blocks = []
    for top in range(0, height, step):
        if cancelled.is_set():
            return None
        tile = image.copy(0, top, width, min(tile_height, height-top))
        data = output_to_data(engine(image_to_bgr(tile)), width, tile.height())
        # 重叠带按中线分配所有权；跨切片边界的文字由包含完整行的切片负责。
        low = top+overlap/2 if top else 0
        high = top+tile_height-overlap/2 if top+tile_height < height else height
        for block in data["blocks"]:
            left, y1, right, y2 = block["bbox"]
            absolute_top, absolute_bottom = top+y1*tile.height(), top+y2*tile.height()
            if not low <= (absolute_top+absolute_bottom)/2 < high:
                continue
            blocks.append(dict(block, bbox=[left, absolute_top/height, right, absolute_bottom/height]))
        if top+tile_height >= height:
            break
    blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    for index, block in enumerate(blocks, 1):
        block["id"] = str(index)
    return {"blocks": blocks, "engine": "RapidOCR", "tiled": True}


class LocalOcrService(QObject):
    completed = Signal(int, object, str)
    delivered = Signal(int, object, str)

    def __init__(self, parent=None, engine_factory=None, model_dir=None):
        super().__init__(parent)
        self.model_dir = Path(model_dir).resolve() if model_dir is not None else MODEL_DIR
        self.engine_factory = engine_factory or (lambda: create_engine(self.model_dir))
        self.engine = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipboard-ocr")
        self.jobs, self.serial, self.closed = {}, 0, False
        self.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def request(self, image):
        if self.closed:
            raise RuntimeError("OCR 服务已关闭")
        self.serial += 1
        job_id, cancelled, started = self.serial, Event(), perf_counter()
        # 副本是已打码选区，工作线程没有 QWidget 或整屏底图。
        future = self.executor.submit(self._run, image.copy(), cancelled, started)
        self.jobs[job_id] = (cancelled, future)

        def finished(result):
            if cancelled.is_set():
                data, error = None, ""
            else:
                try:
                    data, error = result.result(), ""
                except (ImportError, FileNotFoundError):
                    data, error = None, "本地 OCR 组件或模型缺失，请检查安装。可在设置中手动选择 API 识别。"
                except Exception:
                    # 不记录可能包含输入的异常正文或私人识别内容。
                    data, error = None, "本地文字识别未完成，编辑内容已保留。请重试或手动选择 API 识别。"
            try:
                self.completed.emit(job_id, data, error)
            except RuntimeError:
                pass  # 程序退出后忽略已删除界面的迟到任务。
        future.add_done_callback(finished)
        return job_id

    def _run(self, image, cancelled, started):
        if cancelled.is_set():
            return None
        if self.engine is None:
            self.engine = self.engine_factory()
        if cancelled.is_set():
            return None
        data = recognize_image(self.engine, image, cancelled)
        if cancelled.is_set():
            return None
        data["elapsed"] = round(perf_counter() - started, 3)
        return data

    @Slot(int, object, str)
    def _deliver(self, job_id, data, error):
        self.jobs.pop(job_id, None)
        if not self.closed:
            self.delivered.emit(job_id, data, error)

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job:
            job[0].set()
            job[1].cancel()

    def shutdown(self):
        self.closed = True
        for cancelled, future in list(self.jobs.values()):
            cancelled.set()
            future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)


def shared_service():
    app = QApplication.instance()
    model_dir = Path(getattr(app, "_clipboard_ocr_model_dir", MODEL_DIR)).resolve()
    service = getattr(app, "_clipboard_local_ocr", None)
    if service is None or service.closed or service.model_dir != model_dir:
        if service is not None and not service.closed:
            service.shutdown()
        service = LocalOcrService(app, model_dir=model_dir)
        app._clipboard_local_ocr = service
        app.aboutToQuit.connect(service.shutdown)
    return service


class LocalOcrClient(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None, service=None):
        super().__init__(parent)
        self.service = service if service is not None else shared_service()
        self.job_id = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.service.delivered.connect(self._result)

    @property
    def busy(self):
        return self.job_id is not None

    def request(self, image):
        self.cancel()
        self.job_id = self.service.request(image)
        tiles = (image.height()+1119)//1120 if image.height() > max(1280, image.width()*2) else 1
        self.timer.start(min(180000, 30000+max(0, tiles-1)*4000))
        self.busy_changed.emit(True)

    def cancel(self):
        self.timer.stop()
        if self.job_id is not None:
            # 不强杀 ONNX 线程，取消任务编号使迟到结果失效。
            self.service.cancel(self.job_id)
            self.job_id = None
            self.busy_changed.emit(False)

    def _timeout(self):
        self.cancel()
        self.failed.emit("本地识别超时，编辑内容已保留。请缩小选区重试。")

    @Slot(int, object, str)
    def _result(self, job_id, data, error):
        if self.job_id != job_id:
            return
        self.timer.stop()
        self.job_id = None
        self.busy_changed.emit(False)
        if error:
            self.failed.emit(error)
        elif data is not None:
            self.succeeded.emit("ocr", data)
