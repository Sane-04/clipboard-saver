"""采集中的已接受长图预览：按可见像素段绘制，不逐帧重建全尺寸长图。"""
from copy import deepcopy
from math import ceil

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from image_document import move_object, object_rect, paint_object, rect_of, rect_tuple
from ui_theme import STYLE


class PreviewCanvas(QWidget):
    extent_changed = Signal()

    def __init__(self):
        super().__init__()
        self.source_parts = ()
        self.parts, self.layers = [], []
        self.image_width = self.image_height = 0
        self.latest_start = None
        self.viewport_start = None
        self.setMinimumWidth(1)

    @property
    def scale(self):
        return max(1, self.width()-16)/max(1, self.image_width)

    def set_content(self, parts, width, height, added=None, layers=()):
        layers = deepcopy(list(layers))
        if layers != self.layers:
            self.source_parts, self.parts = (), []
            self.layers = layers
        # 已存在的段不会变；撤回只截短段列表，追加只准备新的段。
        common = 0
        while common < min(len(parts), len(self.source_parts)) and parts[common] is self.source_parts[common]:
            common += 1
        self.parts = self.parts[:common]
        y = sum(part.height() for part in parts[:common])
        for raw in parts[common:]:
            image = QImage(raw)
            image.setDevicePixelRatio(1)
            segment = QRectF(0, y, width, image.height())
            relevant = [obj for obj in layers if object_rect(obj).intersects(segment) or
                        ("source_rect" in obj and rect_of(obj["source_rect"]).intersects(segment))]
            if relevant:
                image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                for obj in relevant:
                    obj = deepcopy(obj)
                    move_object(obj, QPointF(0, -y))
                    if "source_rect" in obj:
                        obj["source_rect"] = rect_tuple(rect_of(obj["source_rect"]).translated(0, -y))
                    paint_object(image, obj)
            self.parts.append(image)
            y += raw.height()
        self.source_parts = tuple(parts)
        self.image_width, self.image_height = width, height
        self.latest_start = height-added if added else None
        self.sync_extent()
        self.update()

    def sync_extent(self):
        needed = max(1, ceil(self.image_height*self.scale)+16)
        if self.minimumHeight() != needed:
            self.setMinimumHeight(needed)
            self.extent_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_extent()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#eef1f6"))
        if not self.parts:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.save()
        painter.translate(8, 8)
        painter.scale(self.scale, self.scale)
        top = (event.rect().top()-8)/self.scale
        bottom = (event.rect().bottom()-8)/self.scale
        y = 0
        for part in self.parts:
            # 只绘制当前滚动视口内的段，长度增长不增加整图渲染和分配。
            if y+part.height() >= top and y <= bottom:
                painter.drawImage(0, y, part)
            y += part.height()
        painter.restore()
        if self.latest_start is not None:
            y = 8+self.latest_start*self.scale
            painter.fillRect(QRectF(8, y, self.width()-16, self.image_height*self.scale+8-y),
                             QColor(66, 101, 232, 15))
            painter.setPen(QPen(QColor("#4265e8"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(8, y), QPointF(self.width()-8, y))
        if self.viewport_start is not None:
            painter.setPen(QPen(QColor("#20865d"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(8, 8+self.viewport_start*self.scale, self.width()-16,
                                   (self.image_height-self.viewport_start)*self.scale))


class CapturePreview(QWidget):
    hidden_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("剪贴收藏 · 长截图实时预览")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(260, 220)
        self.resize(320, 460)
        self.live_image = QImage()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("已拼接预览")
        title.setObjectName("panelTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.follow = QPushButton("跟随最新")
        self.follow.setCheckable(True)
        self.follow.setChecked(True)
        self.follow.setToolTip("滚动查看前面内容后，点击这里恢复自动跟随")
        self.follow.setStyleSheet("padding:4px 8px;")
        heading.addWidget(self.follow)
        layout.addLayout(heading)
        self.live_title = QLabel("当前选区画面 · 正在准备")
        self.live_title.setObjectName("muted")
        layout.addWidget(self.live_title)
        self.live_frame = QLabel()
        self.live_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_frame.setFixedHeight(112)
        self.live_frame.setStyleSheet("background:#eef1f6; border:1px solid #dce2ed;")
        layout.addWidget(self.live_frame)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = PreviewCanvas()
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)
        self.dimensions = QLabel("正在准备预览…")
        layout.addWidget(self.dimensions)
        self.note = QLabel("上方为当前画面 · 绿色框为已接入视口 · 蓝线为新增位置")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.follow_timer = QTimer(self)
        self.follow_timer.setSingleShot(True)
        self.follow_timer.timeout.connect(self.follow_latest)
        self.follow.toggled.connect(lambda checked: self.follow_timer.start(0) if checked else None)
        self.canvas.extent_changed.connect(lambda: self.follow_timer.start(0))
        bar = self.scroll.verticalScrollBar()
        bar.sliderPressed.connect(lambda: self.follow.setChecked(False))
        bar.actionTriggered.connect(lambda _: self.follow.setChecked(False))

    def set_content(self, parts, width, height, added=None, layers=()):
        self.canvas.set_content(parts, width, height, added, layers)
        suffix = f" · 最近接入 +{added} 像素" if added else ""
        self.dimensions.setText(f"{width} × {height} 像素{suffix}")
        self.follow_timer.start(0)

    def set_live_frame(self, image, status="正在核对"):
        self.live_image = QImage(image)
        self.live_image.setDevicePixelRatio(1)
        self.live_title.setText(f"当前选区画面 · {status}")
        self.refresh_live()

    def set_live_status(self, status, accepted=False):
        self.live_title.setText(f"当前选区画面 · {status}")
        self.canvas.viewport_start = max(0, self.canvas.image_height-self.live_image.height()) \
            if accepted and not self.live_image.isNull() else None
        self.canvas.update()

    def refresh_live(self):
        if not self.live_image.isNull():
            self.live_frame.setPixmap(QPixmap.fromImage(self.live_image).scaled(
                self.live_frame.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "live_frame"):
            self.refresh_live()

    def follow_latest(self):
        if self.follow.isChecked():
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

    def place_outside(self, selection, available, reserved):
        gap = 12
        width = min(self.width(), available.width()-gap*2)
        height = min(self.height(), available.height()-gap*2)
        candidates = [
            QRect(selection.right()+gap, available.top()+gap,
                  min(width, available.right()-selection.right()-gap*2), height),
            QRect(available.left()+gap, available.top()+gap,
                  min(width, selection.left()-available.left()-gap*2), height),
            QRect(available.right()-width-gap, selection.bottom()+gap, width,
                  min(height, available.bottom()-selection.bottom()-gap*2)),
            QRect(available.right()-width-gap, available.top()+gap, width,
                  min(height, selection.top()-available.top()-gap*2)),
        ]
        for candidate in candidates:
            if candidate.intersects(reserved):
                candidate.setTop(reserved.bottom()+gap)
            if candidate.width() >= self.minimumWidth() and candidate.height() >= self.minimumHeight() and \
                    not candidate.intersects(selection) and available.contains(candidate):
                self.setGeometry(candidate)
                return
        # 选区占满屏幕时先显示在角落；采集控制器会在读取画面前隐藏重叠窗口。
        self.resize(width, height)
        self.move(available.right()-width-gap, available.top()+gap)

    def closeEvent(self, event):
        event.ignore()
        self.hidden_requested.emit()

    def dispose(self):
        self.follow_timer.stop()
        self.hide()
        self.deleteLater()
