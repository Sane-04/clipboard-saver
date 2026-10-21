"""图片底图、像素坐标编辑层、撤销及统一栅格导出。"""

from copy import deepcopy
from math import atan2, cos, sin, pi, ceil

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF, QTextDocument


def rect_of(value):
    return QRectF(*value)


def rect_tuple(rect):
    return (rect.x(), rect.y(), rect.width(), rect.height())


def bounded_rect(rect, bounds, minimum=1):
    rect = rect.normalized().intersected(bounds)
    if rect.width() < minimum or rect.height() < minimum:
        return None
    return rect


def handles(rect):
    return [rect.topLeft(), QPointF(rect.center().x(), rect.top()), rect.topRight(),
            QPointF(rect.right(), rect.center().y()), rect.bottomRight(),
            QPointF(rect.center().x(), rect.bottom()), rect.bottomLeft(),
            QPointF(rect.left(), rect.center().y())]


def resize_rect(rect, handle, point, bounds, minimum=4):
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    x = max(bounds.left(), min(bounds.right(), point.x()))
    y = max(bounds.top(), min(bounds.bottom(), point.y()))
    if handle in (0, 6, 7):
        left = min(x, right - minimum)
    if handle in (2, 3, 4):
        right = max(x, left + minimum)
    if handle in (0, 1, 2):
        top = min(y, bottom - minimum)
    if handle in (4, 5, 6):
        bottom = max(y, top + minimum)
    return QRectF(left, top, right - left, bottom - top).intersected(bounds)


def object_rect(obj):
    if "rect" in obj:
        return rect_of(obj["rect"])
    points = obj["points"]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return QRectF(min(xs), min(ys), max(1, max(xs) - min(xs)), max(1, max(ys) - min(ys)))


def move_object(obj, delta):
    if "rect" in obj:
        obj["rect"] = rect_tuple(rect_of(obj["rect"]).translated(delta))
    else:
        obj["points"] = [(x + delta.x(), y + delta.y()) for x, y in obj["points"]]
    # 译文移动只移动排版框，原文覆盖范围保持固定，避免重新露出原文。


def scale_object(obj, original, target):
    if "rect" in obj:
        obj["rect"] = rect_tuple(target)
    else:
        obj["points"] = [
            (target.x() + (x - original.x()) / original.width() * target.width(),
             target.y() + (y - original.y()) / original.height() * target.height())
            for x, y in obj["points"]]


def estimate_background(image, rect):
    area = rect.toAlignedRect().intersected(image.rect())
    if area.isEmpty():
        return "#ffffff"
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(
        rgba.height(), rgba.bytesPerLine() // 4, 4)[:, :rgba.width(), :3]
    x, y, w, h = area.x(), area.y(), area.width(), area.height()
    patch = data[y:y + h, x:x + w]
    border = np.concatenate((patch[0], patch[-1], patch[:, 0], patch[:, -1]))
    rgb = np.median(border, axis=0).astype(int)
    return QColor(*map(int, rgb)).name()


def estimate_foreground(image, rect, background):
    area = rect.toAlignedRect().intersected(image.rect())
    if area.isEmpty():
        return "#202020"
    patch = image.copy(area).convertToFormat(QImage.Format.Format_RGBA8888)
    data = np.frombuffer(patch.constBits(), dtype=np.uint8).reshape(
        patch.height(), patch.bytesPerLine() // 4, 4)[:, :patch.width(), :3].reshape(-1, 3)
    bg = QColor(background)
    distance = np.linalg.norm(data.astype(float) - [bg.red(), bg.green(), bg.blue()], axis=1)
    # 短数字/标点的字形可能不足框内 15%；分位数为零时不能把白色背景
    # 一起取中位数，否则译文会得到背景色并在覆盖原文后消失。
    tail = data[distance >= max(8, float(np.percentile(distance, 85)))]
    if not len(tail):
        return "#202020" if bg.lightness() >= 128 else "#ffffff"
    return QColor(*map(int, np.median(tail, axis=0))).name()


def text_layout(text, width, size):
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(max(6, round(size)))
    doc.setDefaultFont(font)
    option = doc.defaultTextOption()
    option.setWrapMode(option.WrapMode.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(option)
    doc.setPlainText(text)
    doc.setTextWidth(max(1, width))
    return doc


def fit_font(text, rect, start_size):
    size = max(8, round(start_size))
    while size > 8 and text_layout(text, rect.width(), size).size().height() > rect.height():
        size -= 1
    return size


def text_overflows(obj):
    if obj["kind"] not in ("text", "translation"):
        return False
    rect = rect_of(obj["rect"])
    return text_layout(obj["text"], rect.width(), obj["font_size"]).size().height() > rect.height() + 1


def paint_object(image, obj):
    kind = obj["kind"]
    if kind == "mosaic":
        area = object_rect(obj).toAlignedRect().intersected(image.rect())
        if area.isEmpty():
            return
        strength = max(2, obj.get("strength", 12))
        # 先从当前已渲染图取像素再缩小放大；打码会真实进入 PNG 和 API 图。
        patch = image.copy(area).scaled(max(1, ceil(area.width() / strength)),
                                        max(1, ceil(area.height() / strength)),
                                        Qt.AspectRatioMode.IgnoreAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
        patch = patch.scaled(area.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.FastTransformation)
        painter = QPainter(image)
        painter.drawImage(area.topLeft(), patch)
        painter.end()
        return
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(obj.get("color", "#e54848"))
    pen = QPen(color, obj.get("width", 3), Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if kind == "cover":
        painter.fillRect(object_rect(obj), color)
    elif kind in ("line", "arrow", "freehand"):
        points = [QPointF(*p) for p in obj["points"]]
        painter.drawPolyline(QPolygonF(points))
        if kind == "arrow" and len(points) >= 2:
            tip, start = points[-1], points[0]
            angle = atan2(tip.y() - start.y(), tip.x() - start.x())
            length = max(12, obj.get("width", 3) * 4)
            triangle = QPolygonF([tip,
                tip - QPointF(cos(angle - pi / 6) * length, sin(angle - pi / 6) * length),
                tip - QPointF(cos(angle + pi / 6) * length, sin(angle + pi / 6) * length)])
            painter.setBrush(color)
            painter.drawPolygon(triangle)
    elif kind in ("text", "translation"):
        rect = object_rect(obj)
        if kind == "translation":
            painter.fillRect(rect_of(obj["source_rect"]), QColor(obj["background"]))
        # QTextDocument 的同一排版用于预览/导出和溢出判断；用户可扩大框而不丢字。
        layout = text_layout(obj["text"], rect.width(), obj["font_size"])
        painter.translate(rect.topLeft())
        from PySide6.QtGui import QAbstractTextDocumentLayout, QPalette
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, color)
        painter.setClipRect(QRectF(0, 0, rect.width(), rect.height()))
        layout.documentLayout().draw(painter, context)
    painter.end()


class ImageDocument(QObject):
    changed = Signal()

    def __init__(self, image, crop=None):
        super().__init__()
        self.base = image.copy()
        self.base.setDevicePixelRatio(1)
        self.bounds = QRectF(0, 0, image.width(), image.height())
        self.crop = crop
        self.objects = []
        self.revision = 0
        self.undo_states = []
        self.redo_states = []
        self._cache = None
        self.history_capture = self.history_restore = None

    def state(self):
        state = (self.crop, self.objects)
        if self.history_capture:
            state += (self.history_capture(),)
        return deepcopy(state)

    def restore(self, state):
        self.crop, self.objects = state[:2]
        if len(state) == 3 and self.history_restore:
            self.history_restore(state[2])
        self.changed_now()

    def changed_now(self):
        self.revision += 1
        self._cache = None
        self.changed.emit()

    def record(self, before):
        if before != self.state():
            self.undo_states.append(before)
            self.undo_states = self.undo_states[-80:]
            self.redo_states.clear()
            self.changed_now()

    def add(self, objects):
        before = self.state()
        self.objects.extend(deepcopy(objects))
        self.record(before)

    def undo(self):
        if self.undo_states:
            self.redo_states.append(self.state())
            self.restore(self.undo_states.pop())

    def redo(self):
        if self.redo_states:
            self.undo_states.append(self.state())
            self.restore(self.redo_states.pop())

    def full_image(self):
        if self._cache is None:
            self._cache = self.base.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            for obj in self.objects:
                paint_object(self._cache, obj)
        return self._cache

    def export_rect(self):
        return (rect_of(self.crop) if self.crop else self.bounds).toAlignedRect().intersected(self.base.rect())

    def render(self):
        # OCR、预览和完成共用这一导出；从不上传选区以外的底图。
        result = self.full_image().copy(self.export_rect())
        result.setDevicePixelRatio(1)
        return result
