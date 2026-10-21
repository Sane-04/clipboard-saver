"""可调整的图片编辑器，新截图和历史共用；控件不进入导出像素。"""
from copy import deepcopy
from pathlib import Path
from time import time_ns
from floating_panel import FloatingPanel, DragHandle
from ui_theme import STYLE, icon

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QToolButton, QMenu,
    QVBoxLayout, QWidget,
)

from api_client import ApiClient, OCR_PROMPT, parse_ocr, parse_translations
from translation_session import TranslationSession
from text_results import TextResultsDialog
from window_pin import TopmostButton
from api_settings import SettingsDialog
from local_ocr import LocalOcrClient
from shortcut_settings import DEFAULT_SHORTCUTS, ShortcutSettingsDialog
from data_locations import write_png
from image_document import (
    ImageDocument, bounded_rect, estimate_background, estimate_foreground, fit_font, handles,
    move_object, object_rect, rect_of, rect_tuple, resize_rect, scale_object, text_overflows,
)




def ask_text(parent, initial=""):
    dialog = QDialog(parent)
    dialog.setWindowTitle("文字标注 · Enter 换行")
    dialog.resize(430, 240)
    layout = QVBoxLayout(dialog)
    field = QPlainTextEdit(initial)
    layout.addWidget(field)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加 / 更新")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    field.setFocus()
    return field.toPlainText() if dialog.exec() == QDialog.DialogCode.Accepted else None


class EditorScrollArea(QScrollArea):
    def wheelEvent(self, event):
        if not self.parentWidget().zoom_wheel(event):
            super().wheelEvent(event)


class EditorCanvas(QWidget):
    selection_changed = Signal()
    crop_ready = Signal()
    blocks_changed = Signal()

    def __init__(self, editor, fullscreen=False):
        super().__init__(editor)
        self.editor, self.doc, self.fullscreen = editor, editor.doc, fullscreen
        self.tool = "crop" if fullscreen else "select"
        self.selected = self.selected_block = self.drag = None
        self.before = self.original = self.start = None
        self.pick_background = self.source_mode = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.doc.changed.connect(self.update)

    def transform(self):
        if self.fullscreen:
            # 截图按屏幕 DPR 映射，避免逻辑尺寸取整造成 150% 下多裁一行/列。
            return 1 / self.editor.display_ratio, QPointF(0, 0)
        margin = 0 if self.fullscreen else 20
        scale = self.editor.zoom_factor or min(
            max(1, self.width() - 2 * margin) / self.doc.base.width(),
            max(1, self.height() - 2 * margin) / self.doc.base.height())
        origin = QPointF((self.width() - self.doc.base.width() * scale) / 2,
                         (self.height() - self.doc.base.height() * scale) / 2) + self.editor.zoom_pan
        return scale, origin

    def wheelEvent(self, event):
        if not self.editor.zoom_wheel(event):
            super().wheelEvent(event)

    def to_pixel(self, point):
        scale, origin = self.transform()
        p = (point - origin) / scale
        return QPointF(max(0, min(self.doc.bounds.right(), p.x())),
                       max(0, min(self.doc.bounds.bottom(), p.y())))

    def current_rect(self):
        if self.tool == "ocrbox" and self.selected_block is not None:
            value = self.editor.blocks[self.selected_block].get("rect")
            return rect_of(value) if value else None
        if self.tool == "crop":
            return rect_of(self.doc.crop) if self.doc.crop else None
        if self.selected is not None and self.selected < len(self.doc.objects):
            obj = self.doc.objects[self.selected]
            return rect_of(obj["source_rect"]) if self.source_mode and obj["kind"] == "translation" else object_rect(obj)
        return None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#eceef3"))
        scale, origin = self.transform()
        painter.translate(origin)
        painter.scale(scale, scale)
        painter.drawImage(self.doc.bounds, self.doc.full_image())
        crop = rect_of(self.doc.crop) if self.doc.crop else None
        if self.fullscreen or crop:
            mask = QPainterPath()
            mask.addRect(self.doc.bounds)
            if crop:
                mask.addRect(crop)
            painter.fillPath(mask, QColor(24, 29, 41, 115))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if crop:
            painter.setPen(QPen(QColor("#4265e8"), 1.5 / scale))
            painter.drawRect(crop)
        if self.editor.panel.isVisible() and self.editor.ocr_revision == self.doc.revision:
            for index, block in enumerate(self.editor.blocks):
                if block.get("rect"):
                    painter.setPen(QPen(QColor("#448aff"), (2 if index == self.selected_block else 1) / scale,
                                        Qt.PenStyle.DashLine))
                    painter.drawRect(rect_of(block["rect"]))
        rect = self.current_rect()
        if rect:
            painter.setPen(QPen(QColor("#448aff" if self.tool == "ocrbox" else "#4265e8"), 1.5 / scale))
            painter.drawRect(rect)
            painter.setBrush(QColor("white"))
            for point in handles(rect):
                painter.drawRect(QRectF(point.x() - 4 / scale, point.y() - 4 / scale, 8 / scale, 8 / scale))
        painter.end()

    def hit_handle(self, rect, point):
        if rect is None:
            return None
        scale, _ = self.transform()
        # 短文字框的控制点热区不能覆盖整个框，内部仍需留给移动手势。
        inset = min(6 / scale, rect.width() * .2, rect.height() * .2)
        if rect.adjusted(inset, inset, -inset, -inset).contains(point):
            return None
        for index, handle in enumerate(handles(rect)):
            if (handle - point).manhattanLength() < 13 / scale:
                return index
        tolerance = 6 / scale
        if rect.left() <= point.x() <= rect.right():
            if abs(point.y() - rect.top()) < tolerance:
                return 1
            if abs(point.y() - rect.bottom()) < tolerance:
                return 5
        if rect.top() <= point.y() <= rect.bottom():
            if abs(point.x() - rect.left()) < tolerance:
                return 7
            if abs(point.x() - rect.right()) < tolerance:
                return 3
        return None

    def set_target_rect(self, rect):
        if self.tool == "crop":
            self.doc.crop = rect_tuple(rect)
        elif self.tool == "ocrbox":
            self.editor.blocks[self.selected_block]["rect"] = rect_tuple(rect)
            self.update()
            return
        else:
            obj = self.doc.objects[self.selected]
            if self.source_mode and obj["kind"] == "translation":
                obj["source_rect"] = rect_tuple(rect)
            else:
                scale_object(obj, self.original, rect)
        self.doc.changed_now()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.editor.cancel()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        point = self.to_pixel(event.position())
        if self.pick_background and self.selected is not None:
            x, y = min(self.doc.base.width() - 1, int(point.x())), min(self.doc.base.height() - 1, int(point.y()))
            self.editor.change_style("background", self.doc.full_image().pixelColor(x, y).name())
            self.pick_background = False
            return
        if self.tool == "ocrbox" and self.editor.ocr_revision != self.doc.revision:
            self.editor.api_error("旧文字框已失效，请重新提取。")
            return
        if self.tool == "ocrbox" and self.editor.request_active and self.editor.request_kind != "translate":
            self.editor.cancel_api()
        if self.tool == "ocrbox" and self.hit_handle(self.current_rect(), point) is None:
            # 点击哪一块就选哪一块，不能把另一个框当作当前框的新位置。
            clicked = next((i for i in range(len(self.editor.blocks)-1, -1, -1)
                            if self.editor.blocks[i].get("rect") and
                            (rect_of(self.editor.blocks[i]["rect"]).contains(point) or
                             self.hit_handle(rect_of(self.editor.blocks[i]["rect"]), point) is not None)), None)
            if clicked is not None:
                self.editor.block_list.setCurrentRow(clicked)
        self.before, self.start = self.doc.state(), point
        rect = self.current_rect()
        handle = self.hit_handle(rect, point)
        if self.tool in ("crop", "select", "ocrbox") and handle is not None:
            self.original, self.drag = QRectF(rect), ("resize", handle)
        elif self.tool == "crop":
            if rect and rect.contains(point):
                self.original, self.drag = QRectF(rect), ("move_rect", None)
            else:
                self.drag = ("new_crop", None)
                self.doc.crop = rect_tuple(QRectF(point, point))
        elif self.tool == "ocrbox":
            if self.selected_block is None:
                return
            if rect and rect.contains(point):
                self.original, self.drag = QRectF(rect), ("move_rect", None)
            else:
                self.drag = ("new_block", None)
        elif self.tool == "select":
            if self.source_mode and rect and rect.contains(point):
                self.original, self.drag = QRectF(rect), ("move_rect", None)
                self.doc.changed_now()
                return
            scale, _ = self.transform()
            self.selected = next((i for i in range(len(self.doc.objects) - 1, -1, -1)
                if object_rect(self.doc.objects[i]).adjusted(-7 / scale, -7 / scale, 7 / scale, 7 / scale).contains(point)), None)
            if self.selected is not None:
                self.original, self.drag = deepcopy(self.doc.objects[self.selected]), ("move_object", None)
            self.selection_changed.emit()
        elif self.tool == "text":
            text = ask_text(self.editor)
            if text:
                obj = {"kind": "text", "text": text, "font_size": self.editor.font_size.value(),
                       "color": self.editor.color, "rect": (point.x(), point.y(),
                        max(1, min(300, self.doc.bounds.right() - point.x())),
                        max(1, min(150, self.doc.bounds.bottom() - point.y())))}
                self.doc.add([obj])
                self.selected = len(self.doc.objects) - 1
                self.editor.set_tool("select")
                self.selection_changed.emit()
            return
        else:
            if self.doc.crop and not rect_of(self.doc.crop).contains(point):
                return
            obj = {"kind": self.tool, "color": self.editor.color, "width": self.editor.line_width.value(),
                   "strength": self.editor.mosaic_strength.value()}
            if self.tool in ("cover", "mosaic"):
                obj["rect"] = rect_tuple(QRectF(point, point))
            else:
                obj["points"] = [(point.x(), point.y()), (point.x(), point.y())]
            self.doc.objects.append(obj)
            self.selected = len(self.doc.objects) - 1
            self.drag = ("draw", None)
        if self.drag and self.tool != "ocrbox":
            # 手势开始即使在途识别失效，旧响应不能覆盖新像素。
            self.doc.changed_now()
        self.update()

    def mouseMoveEvent(self, event):
        point = self.to_pixel(event.position())
        if not self.drag:
            handle = self.hit_handle(self.current_rect(), point)
            cursors = [Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeVerCursor,
                       Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeHorCursor] * 2
            self.setCursor(cursors[handle] if handle is not None else Qt.CursorShape.CrossCursor)
            return
        kind, handle = self.drag
        if kind in ("new_crop", "new_block"):
            rect = bounded_rect(QRectF(self.start, point), self.doc.bounds)
            if rect:
                if kind == "new_block":
                    self.editor.blocks[self.selected_block]["rect"] = rect_tuple(rect)
                else:
                    self.doc.crop = rect_tuple(rect)
                    self.doc.changed_now()
        elif kind == "resize":
            self.set_target_rect(resize_rect(self.original, handle, point, self.doc.bounds))
        elif kind == "move_rect":
            delta = point - self.start
            delta.setX(max(-self.original.left(), min(self.doc.bounds.right() - self.original.right(), delta.x())))
            delta.setY(max(-self.original.top(), min(self.doc.bounds.bottom() - self.original.bottom(), delta.y())))
            self.set_target_rect(self.original.translated(delta))
        elif kind == "move_object":
            obj = deepcopy(self.original)
            bounds, delta = object_rect(obj), point - self.start
            delta.setX(max(-bounds.left(), min(self.doc.bounds.right() - bounds.right(), delta.x())))
            delta.setY(max(-bounds.top(), min(self.doc.bounds.bottom() - bounds.bottom(), delta.y())))
            move_object(obj, delta)
            self.doc.objects[self.selected] = obj
            self.doc.changed_now()
        elif kind == "draw":
            obj = self.doc.objects[self.selected]
            if obj["kind"] in ("cover", "mosaic"):
                obj["rect"] = rect_tuple(QRectF(self.start, point).normalized())
            elif obj["kind"] == "freehand":
                obj["points"].append((point.x(), point.y()))
            else:
                obj["points"][-1] = (point.x(), point.y())
            self.doc.changed_now()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.drag:
            return
        kind = self.drag[0]
        self.mouseMoveEvent(event)
        self.drag = None
        if self.tool == "ocrbox":
            self.doc.record(self.before)
            self.blocks_changed.emit()
        else:
            if kind == "new_crop" and (not self.doc.crop or min(self.doc.crop[2:]) < 2):
                self.doc.crop = self.before[0]
            if kind == "draw" and self.selected is not None:
                rect = object_rect(self.doc.objects[self.selected])
                if rect.width() < 2 and rect.height() < 2:
                    self.doc.objects.pop()
                    self.selected = None
            self.doc.record(self.before)
            if self.doc.crop:
                self.crop_ready.emit()
        self.selection_changed.emit()
        self.update()
        if self.editor.pending_translation_preview:
            self.editor.pending_translation_preview = False
            self.editor.refresh_blocks()
            self.editor.complete_translation_preview()

    def mouseDoubleClickEvent(self, event):
        if self.selected is not None:
            obj = self.doc.objects[self.selected]
            if obj["kind"] in ("text", "translation"):
                text = ask_text(self.editor, obj["text"])
                if text is not None:
                    self.editor.change_style("text", text)


class ImageEditor(QDialog):
    selected = Signal(object)
    cancelled = Signal()
    text_copied = Signal(str)
    long_capture_requested = Signal()

    def __init__(self, image, settings_store=None, parent=None, *, fullscreen=False, geometry=None,
                 local_service=None, shortcut_store=None, long_image=False, long_capture_enabled=False,
                 update_handler=None):
        super().__init__(parent)
        self.fullscreen, self.store = fullscreen, settings_store
        self.update_handler = update_handler
        self.zoom_factor, self.zoom_serial = 0, 0
        self.zoom_pan = QPointF()
        self.long_image = long_image or image.height() > image.width()*2
        self.long_capture_enabled = fullscreen or long_capture_enabled
        self.doc = ImageDocument(image, None if fullscreen else (0, 0, image.width(), image.height()))
        self.blocks, self.translation_ids = [], []
        self.translation_id_map = self.translation_originals = None
        self.translation_session = None
        self.translation_retry_ids = set()
        self.translation_selection = None
        self.translation_notice = ""
        self.ocr_revision = self.request_revision = self.request_offset = self.request_size = None
        self.finished = self.loading_fields = False
        self.applying_translation = False
        self.translation_source = None
        self.ocr_session = 0
        self.ocr_source_state = None
        self.translation_cache = {}
        self.shortcut_store = shortcut_store
        self.pending_translation_preview = self.rebuilding_block_list = False
        self.full_text_dialog = None
        self.doc.history_capture = self.capture_text_history
        self.doc.history_restore = self.restore_text_history
        self.color = "#e54848"
        self.setWindowTitle("剪贴收藏 · 图片编辑")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(STYLE)
        if fullscreen:
            self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                                Qt.WindowType.WindowStaysOnTopHint)
            self.setGeometry(geometry)
        else:
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint |
                                Qt.WindowType.WindowCloseButtonHint)
            self.setMinimumSize(760, 490)
            available = self.screen().availableGeometry()
            self.resize(min(1160, available.width()-40), min(760, available.height()-70))
        self.canvas = EditorCanvas(self, fullscreen)
        if not fullscreen:
            self.scroll = EditorScrollArea(self)
            self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.scroll.setWidgetResizable(True)
            self.scroll.setWidget(self.canvas)
        self.panel = self.make_panel()
        self.properties = self.make_properties()
        self.tools = self.make_tools()
        if not fullscreen:
            # 图标增加后最小窗口仍应完整显示长截图、取消和完成按钮。
            self.setMinimumWidth(max(self.minimumWidth(), self.tools.sizeHint().width()+24))
        self.status = QLabel(self)
        self.status.setObjectName("muted")
        self.status.setStyleSheet("background: #f7f8fa; padding: 4px 8px; border-radius: 5px;")
        self.client = ApiClient(self)
        self.client.succeeded.connect(self.api_result)
        self.client.failed.connect(self.api_error)
        self.client.busy_changed.connect(self.api_busy)
        self.local_client = LocalOcrClient(self, service=local_service)
        self.local_client.succeeded.connect(self.api_result)
        self.local_client.failed.connect(self.api_error)
        self.local_client.busy_changed.connect(self.api_busy)
        self.doc.changed.connect(self.document_changed)
        self.canvas.crop_ready.connect(self.show_tools)
        self.canvas.selection_changed.connect(self.canvas_selection_changed)
        self.canvas.blocks_changed.connect(self.load_block_fields)
        self.panel.visibility_changed.connect(self.layout_panels)
        self.panel.size_changed.connect(self.layout_panels)
        self.properties.visibility_changed.connect(self.layout_panels)
        self.panel.hide()
        self.properties.hide()
        if fullscreen:
            self.tools.hide()
            self.status.hide()
            self.hint = QLabel("拖动框选 · 松开后可调整 · Esc / 右键取消", self)
            self.hint.setStyleSheet("background:#202633; color:white; padding:8px 12px; border-radius:7px;")
            self.hint.adjustSize()
        else:
            self.tools.show()
        self.shortcuts = []
        self.configure_shortcuts()
        if self.shortcut_store:
            self.shortcut_store.changed.connect(self.configure_shortcuts)
        self.update_context()
        self.update_status()
        self.layout_panels()
        if not fullscreen and self.long_image:
            self.set_zoom(min(1, max(.1, (self.width()-70)/image.width())))

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def make_tools(self):
        frame = FloatingPanel(self, name="tools")
        body = QWidget()
        body.setObjectName("toolBody")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(7, 6, 7, 6)
        outer.setSpacing(5)
        row = QHBoxLayout()
        row.setSpacing(3)
        frame.handle = DragHandle(frame)
        row.addWidget(frame.handle)
        self.tool_group, self.tool_buttons = QButtonGroup(self), {}
        tools = [("选区", "crop", "crop"), ("移动对象", "select", "select"),
                 ("实色遮盖", "cover", "cover"), ("马赛克", "mosaic", "mosaic"),
                 ("划线", "freehand", "pen"), ("直线", "line", "line"),
                 ("箭头", "arrow", "arrow"), ("文字", "text", "text")]
        for label, name, glyph in tools:
            button = QToolButton(body)
            button.setProperty("iconOnly", True)
            if name in ("mosaic", "freehand"):
                button.setProperty("splitMenu", True)
            button.setIcon(icon(glyph))
            button.setIconSize(QSize(20, 20))
            button.setToolTip(label)
            button.setCheckable(True)
            button.setFixedSize(34, 34)
            button.clicked.connect(lambda checked=False, tool=name: self.set_tool(tool))
            self.tool_group.addButton(button)
            self.tool_buttons[name] = button
            if name in ("cover", "line"):
                button.hide()
            else:
                row.addWidget(button)
        for base, options in [("mosaic", [("马赛克", "mosaic"), ("实色遮盖", "cover")]),
                              ("freehand", [("划线", "freehand"), ("直线", "line")])]:
            menu = QMenu(self)
            for label, value in options:
                menu.addAction(label, lambda checked=False, tool=value: self.set_tool(tool))
            self.tool_buttons[base].setMenu(menu)
            self.tool_buttons[base].setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            self.tool_buttons[base].setFixedWidth(46)
        self.tool_buttons[self.canvas.tool].setChecked(True)
        def separator():
            line = QFrame()
            line.setObjectName("toolSeparator")
            line.setFixedSize(1, 22)
            row.addSpacing(5)
            row.addWidget(line)
            row.addSpacing(5)
        separator()
        self.history_tools = QWidget(body)
        history_row = QHBoxLayout(self.history_tools)
        history_row.setContentsMargins(0, 0, 0, 0)
        history_row.setSpacing(3)
        self.history_buttons = {}
        for label, glyph, callback in [("撤销", "undo", self.undo), ("重做", "redo", self.redo),
                                       ("删除选中对象", "delete", self.delete_object)]:
            button = QToolButton()
            button.setProperty("iconOnly", True)
            button.setProperty("historyAction", True)
            button.setIcon(icon(glyph))
            button.setIconSize(QSize(20, 20))
            button.setToolTip(label)
            button.setFixedSize(32, 34)
            button.clicked.connect(callback)
            history_row.addWidget(button)
            self.history_buttons[glyph] = button
        row.addWidget(self.history_tools)
        separator()
        self.long_capture_button = QToolButton(body)
        self.long_capture_button.setObjectName("longScreenshot")
        self.long_capture_button.setText(
            "继续长截图" if self.long_image and self.long_capture_enabled and not self.fullscreen else "长截图")
        self.long_capture_button.setIcon(icon("long-shot", "#4265e8"))
        self.long_capture_button.setIconSize(QSize(22, 22))
        self.long_capture_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.long_capture_button.setFixedHeight(34)
        self.long_capture_button.setEnabled(self.long_capture_enabled)
        self.long_capture_button.setToolTip(
            "沿用当前截图区域，直接开始手动滚动采集" if self.long_capture_enabled else
            "历史或外部图片没有屏幕选区；请用 Ctrl+Alt+X 截图后开始长截图")
        self.long_capture_button.clicked.connect(self.request_long_capture)
        row.addWidget(self.long_capture_button)
        self.text_entry = self.button(row, "文本提取与翻译", self.open_text_panel)
        settings = QToolButton()
        settings.setProperty("iconOnly", True)
        settings.setProperty("smallAction", True)
        settings.setIcon(icon("more"))
        settings.setIconSize(QSize(20, 20))
        settings.setToolTip("设置与图层操作")
        menu = QMenu(self)
        menu.addAction("文字识别与 API 设置", self.open_settings)
        menu.addAction("快捷键设置", self.open_shortcut_settings).setEnabled(self.shortcut_store is not None)
        menu.addAction("另存为 PNG…", self.save_png)
        self.delete_menu_action = menu.addAction("删除选中对象", self.delete_object)
        menu.addAction("位置与样式", self.show_properties)
        zoom_menu = menu.addMenu("显示比例")
        zoom_menu.setToolTip("画布上按 Ctrl + 向上滚轮放大，Ctrl + 向下滚轮缩小")
        for label, value in [("适应窗口", 0), ("100%", 1), ("150%", 1.5), ("200%", 2)]:
            action = zoom_menu.addAction(label)
            action.setEnabled(not self.fullscreen)
            action.triggered.connect(lambda checked=False, factor=value: self.set_zoom(factor))
        settings.setMenu(menu)
        settings.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        settings.setFixedSize(29, 34)
        row.addWidget(settings)
        self.button(row, "取消", self.cancel).setObjectName("quiet")
        self.commit_button = self.button(row, "完成", self.commit)
        self.commit_button.setObjectName("commit")
        outer.addLayout(row)
        self.context = QWidget()
        context = QHBoxLayout(self.context)
        context.setContentsMargins(23, 0, 0, 0)
        context.setSpacing(8)
        self.color_button = self.button(context, "颜色", self.choose_color)
        self.style_groups = {}
        for name, label, low, high, value in [("line_width", "线宽", 1, 60, 3),
                                           ("font_size", "字号", 6, 240, 24),
                                           ("mosaic_strength", "强度", 2, 100, 12)]:
            group = QWidget()
            group_layout = QHBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(5)
            group_layout.addWidget(QLabel(label))
            field = QSpinBox()
            field.setRange(low, high)
            field.setValue(value)
            field.setFixedWidth(72)
            setattr(self, name, field)
            group_layout.addWidget(field)
            context.addWidget(group)
            self.style_groups[name] = group
        self.line_width.valueChanged.connect(lambda value: self.change_style("width", value))
        self.font_size.valueChanged.connect(lambda value: self.change_style("font_size", value))
        self.mosaic_strength.valueChanged.connect(lambda value: self.change_style("strength", value))
        context.addStretch()
        self.style_detail_button = self.button(context, "位置与样式", self.show_properties)
        self.style_detail_button.setObjectName("quiet")
        if self.update_handler is not None:
            self.save_mode = QComboBox()
            self.save_mode.addItem("另存为新记录", "new")
            self.save_mode.addItem("更新原记录", "update")
            self.save_mode.setToolTip("点击“完成”时采用的保存方式；更新原记录保留日期与标记，各日期中的同一条内容同步更新")
            context.addWidget(self.save_mode)
        if not self.fullscreen:
            self.topmost_button = TopmostButton(self)
            context.addWidget(self.topmost_button)
        outer.addWidget(self.context)
        frame.outer.addWidget(body)
        return frame

    def request_long_capture(self):
        if self.long_capture_enabled and not self.finished and self.doc.crop:
            self.long_capture_requested.emit()

    def suspend_for_capture(self):
        """暂时保留编辑会话；采集取消时能回到同一选区和图层。"""
        self.close_full_text()
        self.client.cancel()
        self.local_client.cancel()
        self.hide()

    def retire_after_capture(self):
        """新长图接管后关闭旧编辑器，不触发取消或提交。"""
        self.finished = True
        self.client.cancel()
        self.local_client.cancel()
        self.close()

    def make_panel(self):
        frame = FloatingPanel(self, "文本提取与翻译", resizable=True)
        controls = QWidget()
        controls.setObjectName("panelBody")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(14, 8, 14, 8)
        controls_layout.setSpacing(6)
        language_row = QHBoxLayout()
        language_row.addWidget(QLabel("目标语言"))
        self.language = QComboBox()
        self.language.setObjectName("targetLanguage")
        self.language.setEditable(True)
        self.language.addItems(["简体中文", "繁體中文", "English", "日本語", "한국어",
                                "Français", "Deutsch", "Español", "Русский"])
        self.language.setToolTip("展开选择目标语言，也可直接输入其他语言")
        if self.store:
            self.language.setCurrentText(self.store.settings.language)
        self.language.currentTextChanged.connect(self.refresh_text_controls)
        language_row.addWidget(self.language, 1)
        controls_layout.addLayout(language_row)
        actions = QHBoxLayout()
        self.translate_button = self.button(actions, "翻译", self.translate)
        self.translate_button.setObjectName("primary")
        self.ocr_button = self.button(actions, "重新提取", self.extract)
        controls_layout.addLayout(actions)
        frame.outer.addWidget(controls)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("panelBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 0, 14, 16)
        layout.setSpacing(6)
        self.panel_note = QLabel("进入后自动提取，翻译由你决定。")
        self.panel_note.setObjectName("muted")
        self.panel_note.setWordWrap(True)
        layout.addWidget(self.panel_note)
        self.cancel_request = self.button(layout, "取消请求", self.cancel_api)
        self.cancel_request.hide()
        results_row = QHBoxLayout()
        results_row.addWidget(QLabel("文字块 · 勾选翻译"), 1)
        self.full_text_button = self.button(results_row, "全部文字", self.open_full_text)
        self.full_text_button.setObjectName("quiet")
        self.full_text_button.setToolTip("打开全部原文与译文，支持跨行全选和一次复制")
        results_row.setSpacing(4)
        layout.addLayout(results_row)
        self.block_list = QListWidget()
        self.block_list.setFixedHeight(66)
        self.block_list.currentRowChanged.connect(self.select_block)
        self.block_list.itemClicked.connect(lambda item: self.select_block(self.block_list.row(item)))
        self.block_list.itemChanged.connect(self.block_checked)
        layout.addWidget(self.block_list)
        self.text_splitter = QSplitter(Qt.Orientation.Vertical)
        self.text_splitter.setObjectName("textSplitter")
        self.text_splitter.setChildrenCollapsible(False)
        self.text_splitter.setHandleWidth(10)
        # 滚动区按可用高度分配文字空间，不能被两个输入框的默认 sizeHint 撑高。
        self.text_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.text_splitter.setMinimumHeight(190)
        for name, title, hint, copy_callback in [
            ("source_text", "原文", "进入后自动提取原文", self.copy_ocr),
            ("translated_text", "译文", "点击翻译后显示译文", self.copy_translated)]:
            group = QWidget()
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(4)
            row = QHBoxLayout()
            row.addWidget(QLabel(title + " · 当前块"))
            row.addStretch()
            button = self.button(row, "复制" + title, copy_callback)
            button.setToolTip("复制所有已勾选文字块的" + title + "；复制未勾选的文字可打开“全部文字”")
            button.setObjectName("quiet")
            button.setStyleSheet("padding: 2px 6px; min-height: 18px; border: 0;")
            setattr(self, "copy_source_button" if name == "source_text" else "copy_translation_button", button)
            group_layout.addLayout(row)
            field = QPlainTextEdit()
            field.setPlaceholderText(hint)
            field.setMinimumHeight(60)
            field.textChanged.connect(self.text_edited)
            setattr(self, name, field)
            group_layout.addWidget(field, 1)
            self.text_splitter.addWidget(group)
        # 分隔位置只设置一次，校对和请求状态刷新不能重置用户分配的文字空间。
        self.text_splitter.setSizes([130, 130])
        self.text_splitter.setStretchFactor(0, 1)
        self.text_splitter.setStretchFactor(1, 1)
        self.text_splitter.handle(1).setCursor(Qt.CursorShape.SizeVerCursor)
        self.text_splitter.handle(1).setToolTip("上下拖动，调整原文与译文区域的高度")
        layout.addWidget(self.text_splitter, 1)
        self.apply_button = self.button(layout, "更新图片预览", self.apply_translations)
        detail_row = QHBoxLayout()
        self.position_button = self.button(detail_row, "校对文字位置", lambda: self.set_tool("ocrbox"))
        self.position_button.setObjectName("quiet")
        self.button(detail_row, "位置与样式", self.show_properties).setObjectName("quiet")
        layout.addLayout(detail_row)
        note = QLabel("完成后复制并保存图片")
        note.setObjectName("muted")
        layout.addWidget(note)
        scroll.setWidget(body)
        frame.outer.addWidget(scroll)
        return frame

    def make_properties(self):
        frame = FloatingPanel(self, "位置与样式")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("panelBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(10)
        self.object_text_group = QWidget()
        text_layout = QVBoxLayout(self.object_text_group)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.addWidget(QLabel("选中图层的文字"))
        self.object_text = QPlainTextEdit()
        self.object_text.setMaximumHeight(90)
        text_layout.addWidget(self.object_text)
        self.button(text_layout, "更新文字", lambda: self.change_style("text", self.object_text.toPlainText()))
        layout.addWidget(self.object_text_group)
        layout.addWidget(QLabel("位置与大小"))
        self.rect_fields = []
        for labels in [("X", "Y"), ("宽", "高")]:
            row = QHBoxLayout()
            for label in labels:
                field = QDoubleSpinBox()
                field.setRange(0, 100000)
                field.setDecimals(1)
                field.setPrefix(label + " ")
                self.rect_fields.append(field)
                row.addWidget(field)
            layout.addLayout(row)
        self.button(layout, "更新位置与大小", self.apply_rect_fields)
        self.translation_properties = QWidget()
        translation_layout = QVBoxLayout(self.translation_properties)
        translation_layout.setContentsMargins(0, 0, 0, 0)
        self.source_checkbox = QCheckBox("调整原文覆盖范围")
        self.source_checkbox.setToolTip("译文移动不改变原文覆盖框；勾选后单独调整覆盖范围。")
        self.source_checkbox.toggled.connect(self.source_mode_changed)
        translation_layout.addWidget(self.source_checkbox)
        row = QHBoxLayout()
        self.button(row, "覆盖背景色", self.choose_background)
        self.button(row, "在图上取色", self.pick_background)
        translation_layout.addLayout(row)
        layout.addWidget(self.translation_properties)
        self.button(layout, "删除选中图层", self.delete_object).setObjectName("quiet")
        layout.addStretch()
        scroll.setWidget(body)
        frame.outer.addWidget(scroll)
        return frame

    def resizeEvent(self, event):
        if event is not None:
            super().resizeEvent(event)
        self.layout_panels()

    def layout_panels(self):
        if not hasattr(self, "tools") or not hasattr(self, "status"):
            return
        if self.fullscreen:
            self.canvas.setGeometry(self.rect())
        width = min(self.width()-16, self.tools.sizeHint().width())
        height = self.tools.sizeHint().height()
        x, y = 12, 10
        if self.fullscreen:
            y = self.height() - height - 45
            if self.doc.crop:
                scale, origin = self.canvas.transform()
                crop = rect_of(self.doc.crop)
                x = round(crop.left()*scale)
                below, above = round(crop.bottom()*scale)+12, round(crop.top()*scale)-height-12
                if below + height < self.height()-40:
                    y = below
                elif above > 52:
                    y = above
            if hasattr(self, "hint"):
                self.hint.move(max(8, (self.width()-self.hint.width())//2), 10)
                self.hint.raise_()
        self.tools.place(x, y, width, height)
        panel_top = 10 if self.fullscreen else height+26
        if not self.fullscreen:
            # 历史编辑窗给顶部工具栏留出独立区域；加宽面板和小窗口都不能挡住完成/撤销。
            self.panel.available_bounds = QRect(8, panel_top, self.width()-16,
                                                max(1, self.height()-panel_top-36))
        self.panel.place(self.width()-342, panel_top, min(328, self.width()-16), min(680, self.height()-48))
        reserve = self.panel.width()+14 if self.panel.isVisible() and not self.fullscreen else 0
        if not self.fullscreen:
            top = height+26 if self.long_image else 0
            self.scroll.setGeometry(0, top, max(1, self.width()-reserve), max(1, self.height()-36-top))
        self.properties.place(12, self.tools.y()+height+9, min(320, self.width()-16), min(390, self.height()-48))
        self.status.setGeometry(10, self.height()-32, self.width()-20, 26)
        self.tools.raise_()
        self.panel.raise_()
        self.properties.raise_()
        self.status.raise_()
        self.canvas.update()

    def show_tools(self):
        if self.fullscreen:
            self.tools.show()
            self.status.show()
            self.hint.hide()
        self.layout_panels()

    def set_tool(self, tool):
        self.canvas.tool, self.canvas.source_mode = tool, False
        if tool != "select":
            # 切换到新绘制工具时设置下一笔的样式，不能误改上一个选中的对象。
            self.canvas.selected = None
        self.source_checkbox.setChecked(False)
        self.tool_group.setExclusive(False)
        visible = {"cover": "mosaic", "line": "freehand"}.get(tool, tool)
        for name, button in self.tool_buttons.items():
            button.setChecked(name in (tool, visible))
        self.tool_group.setExclusive(True)
        if tool == "ocrbox":
            self.load_block_fields()
        self.canvas.update()
        self.update_context()
        self.update_status()

    def update_context(self):
        if not hasattr(self, "context"):
            return
        kind = self.canvas.tool
        index = self.canvas.selected
        if kind == "select" and index is not None and index < len(self.doc.objects):
            kind = self.doc.objects[index]["kind"]
        color = kind in ("cover", "freehand", "line", "arrow", "text", "translation")
        self.color_button.setVisible(color)
        self.style_groups["line_width"].setVisible(kind in ("freehand", "line", "arrow"))
        self.style_groups["font_size"].setVisible(kind in ("text", "translation"))
        self.style_groups["mosaic_strength"].setVisible(kind == "mosaic")
        self.style_detail_button.setVisible(color or kind == "mosaic")
        self.context.setVisible(color or kind == "mosaic" or not self.fullscreen)
        self.object_text_group.setVisible(kind in ("text", "translation"))
        self.translation_properties.setVisible(kind == "translation")
        self.layout_panels()

    def set_zoom(self, factor, *, pixel=None, position=None):
        if self.fullscreen:
            return
        self.zoom_serial += 1
        token = self.zoom_serial
        self.zoom_factor = max(.05, min(8, factor)) if factor else 0
        self.zoom_pan = QPointF()
        if not factor:
            self.canvas.setMinimumSize(0, 0)
            self.scroll.setWidgetResizable(True)
        else:
            self.scroll.setWidgetResizable(True)
            size = QSize(round(self.doc.base.width()*self.zoom_factor + 40),
                         round(self.doc.base.height()*self.zoom_factor + 40))
            self.canvas.setMinimumSize(size)
            self.canvas.resize(size.expandedTo(self.scroll.viewport().size()))
        self.canvas.update()
        self.update_status()
        if pixel is not None:
            def anchor():
                if token != self.zoom_serial or self.finished:
                    return
                # 只移动视口来保持鼠标下的原像素位置；底图、图层、撤销历史完全不变。
                scale, origin = self.canvas.transform()
                mapped = QPointF(self.canvas.mapTo(self.scroll.viewport(), QPoint())) + origin + pixel*scale
                delta = mapped - position
                horizontal, vertical = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
                horizontal.setValue(horizontal.value() + round(delta.x()))
                vertical.setValue(vertical.value() + round(delta.y()))
                # 图片小于视口时没有对应滚动条。利用空白边距保持锚点，不能被居中布局拉走。
                mapped = QPointF(self.canvas.mapTo(self.scroll.viewport(), QPoint())) + origin + pixel*scale
                residual = mapped-position
                free_x = max(0, (self.canvas.width()-self.doc.base.width()*scale-40)/2)
                free_y = max(0, (self.canvas.height()-self.doc.base.height()*scale-40)/2)
                self.zoom_pan = QPointF(
                    max(-free_x, min(free_x, self.zoom_pan.x()-residual.x())) if not horizontal.maximum() else 0,
                    max(-free_y, min(free_y, self.zoom_pan.y()-residual.y())) if not vertical.maximum() else 0)
                self.canvas.update()
            anchor()
            # 滚动条出现/消失会触发布局，下一轮再校准；新滚轮事件使旧校准自动失效。
            QTimer.singleShot(0, self, anchor)

    def zoom_wheel(self, event):
        if self.fullscreen or not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        delta = event.angleDelta().y()
        steps = delta / 120 if delta else event.pixelDelta().y() / 60
        event.accept()
        if not steps:
            return True
        position = QPointF(self.scroll.viewport().mapFromGlobal(event.globalPosition().toPoint()))
        canvas_position = QPointF(self.canvas.mapFrom(self.scroll.viewport(), position.toPoint()))
        scale, origin = self.canvas.transform()
        pixel = (canvas_position - origin) / scale
        factor = max(.05, min(8, scale * 1.2**max(-10, min(10, steps))))
        self.set_zoom(factor, pixel=pixel, position=position)
        return True

    def configure_shortcuts(self, *_):
        for shortcut in self.shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.shortcuts = []
        bindings = self.shortcut_store.bindings if self.shortcut_store else DEFAULT_SHORTCUTS
        actions = {"commit": self.commit, "undo": self.undo, "redo": self.redo, "delete": self.delete_object}
        keys = [("Escape", self.cancel, False)]
        for name, action in actions.items():
            keys.append((bindings[name], action, name == "delete"))
        commit = bindings["commit"]
        if commit.endswith("Return") or commit.endswith("Enter"):
            alias = commit[:-6]+"Enter" if commit.endswith("Return") else commit[:-5]+"Return"
            if alias not in bindings.values():
                keys.append((alias, self.commit, False))
        if bindings["redo"] == DEFAULT_SHORTCUTS["redo"] and "Ctrl+Shift+Z" not in bindings.values():
            keys.append(("Ctrl+Shift+Z", self.redo, False))
        for key, action, canvas_only in keys:
            for parent in (self.canvas, self.block_list) if canvas_only else (self,):
                shortcut = QShortcut(QKeySequence(key), parent)
                if canvas_only:
                    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
                shortcut.activated.connect(action)
                self.shortcuts.append(shortcut)
        for name in ("undo", "redo", "delete"):
            self.history_buttons[name].setToolTip(
                f'{"删除选中对象" if name == "delete" else "撤销" if name == "undo" else "重做"}  {bindings[name]}')
        self.delete_menu_action.setText(f'删除选中对象  {bindings["delete"]}')
        self.commit_button.setToolTip(f'完成并复制保存  {bindings["commit"]}')

    def open_shortcut_settings(self):
        if self.shortcut_store:
            ShortcutSettingsDialog(self.shortcut_store, self).exec()

    def capture_text_history(self):
        return {"session": self.ocr_session, "blocks": self.blocks}

    def restore_text_history(self, state):
        # 旧识别会话的撤销记录不能盖掉后来重新提取的文字；同一会话的框/删除可恢复。
        if state["session"] == self.ocr_session:
            latest = {block["id"]: block for block in self.blocks}
            self.blocks = deepcopy(state["blocks"])
            for block in self.blocks:
                if block["id"] in latest:
                    # 图像/框的撤销不能回退用户后来校对的文字。
                    block["text"] = latest[block["id"]]["text"]
                    block["translated"] = latest[block["id"]]["translated"]
                elif not block["translated"] and block["id"] in self.translation_cache:
                    block["translated"] = self.translation_cache[block["id"]]
            self.refresh_blocks()

    def recognition_source_state(self):
        # 本轮生成的译文属于排版层。底图、裁切、其他标注或上一轮译文变了才需重新识别。
        return (self.doc.crop, [obj for obj in self.doc.objects
                if not (obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session)])

    def canvas_selection_changed(self):
        index = self.canvas.selected
        if index is not None and index < len(self.doc.objects):
            obj = self.doc.objects[index]
            if obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session:
                row = next((i for i, block in enumerate(self.blocks) if block["id"] == obj.get("block_id")), None)
                if row is not None:
                    self.block_list.setCurrentRow(row)
        self.load_object_fields()

    def show_panel(self):
        # 在途结果保留到会话，用户刚收起的面板不因迟到完成而重新弹出。
        if not self.panel.user_hidden:
            self.panel.show()
            self.layout_panels()

    def open_text_panel(self):
        self.panel.user_hidden = False
        self.show_panel()
        # 再次进入复用有效结果；在途重复进入只展示进度，不制造第二个识别请求。
        if self.request_active:
            return
        if self.ocr_revision == self.doc.revision:
            self.set_tool("ocrbox")
            self.refresh_text_controls()
        else:
            self.extract()

    def show_properties(self):
        self.properties.user_hidden = False
        self.properties.show()
        self.load_object_fields()
        self.layout_panels()

    def text_edited(self):
        if not self.loading_fields:
            self.save_block_text()
            self.refresh_text_controls()

    def open_full_text(self):
        self.save_block_text()
        if self.ocr_revision != self.doc.revision or not self.blocks:
            return
        if self.full_text_dialog is None:
            self.full_text_dialog = TextResultsDialog(self)
            self.full_text_dialog.text_copied.connect(self.text_copied.emit)
            self.full_text_dialog.finished.connect(lambda _: setattr(self, "full_text_dialog", None))
        dialog = self.full_text_dialog
        dialog.set_results(self.blocks, True, self.ocr_session)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.select_all()

    def close_full_text(self):
        if self.full_text_dialog is not None:
            self.full_text_dialog.close()

    def copy_translated(self):
        self.save_block_text()
        if self.ocr_revision != self.doc.revision:
            self.api_error("图片已改变，请重新提取。")
            return
        value = "\n".join(b["translated"] for b in self.blocks if b["apply"] and b["translated"].strip())
        if value:
            self.text_copied.emit(value)
            self.status.setText("所选译文已复制。")

    def refresh_text_controls(self):
        if not hasattr(self, "client"):
            return
        valid = self.ocr_revision == self.doc.revision
        busy = self.request_active
        selected = [b for b in self.blocks if b["apply"]]
        self.ocr_button.setEnabled(not busy)
        self.translate_button.setEnabled(valid and bool(selected) and not busy)
        retry = self.translation_retry_ids and self.translation_selection == (
            self.language.currentText().strip() or "简体中文",
            tuple((b["id"], b["text"]) for b in selected))
        self.translate_button.setText("重试未完成" if retry else "翻译所选")
        self.copy_source_button.setEnabled(valid and bool(selected) and not busy)
        self.copy_translation_button.setEnabled(valid and any(b["translated"].strip() for b in selected) and not busy)
        self.full_text_button.setEnabled(valid and bool(self.blocks) and not busy)
        if self.full_text_dialog is not None:
            self.full_text_dialog.set_results(self.blocks, valid, self.ocr_session)
        has_layers = any(o.get("ocr_session") == self.ocr_session for o in self.doc.objects if o["kind"] == "translation")
        self.apply_button.setEnabled(valid and not busy and (any(b["translated"].strip() for b in selected) or has_layers))
        self.block_list.setEnabled(valid and (not busy or getattr(self, "request_kind", "") == "translate"))
        previous_signals = self.block_list.blockSignals(True)
        for row in range(self.block_list.count()):
            item = self.block_list.item(row)
            flags = item.flags()
            item.setFlags(flags & ~Qt.ItemFlag.ItemIsUserCheckable if busy else flags | Qt.ItemFlag.ItemIsUserCheckable)
        self.block_list.blockSignals(previous_signals)
        self.position_button.setEnabled(valid and (not busy or getattr(self, "request_kind", "") == "translate"))
        self.cancel_request.setVisible(busy)

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.color), self, "标注 / 译文前景色")
        if color.isValid():
            self.color = color.name()
            self.change_style("color", self.color)
            self.color_button.setStyleSheet(f"color:{self.color};")

    def choose_background(self):
        color = QColorDialog.getColor(QColor("#ffffff"), self, "原文覆盖背景色")
        if color.isValid():
            self.change_style("background", color.name())

    def pick_background(self):
        index = self.canvas.selected
        if index is not None and self.doc.objects[index]["kind"] == "translation":
            self.canvas.pick_background = True
            self.status.setText("请在图片上点击背景颜色。")
        else:
            self.status.setText("先用移动工具选中译文图层，再取色。")

    def source_mode_changed(self, checked):
        self.canvas.source_mode = checked
        self.load_object_fields()
        self.canvas.update()

    def change_style(self, name, value):
        index = self.canvas.selected
        if self.loading_fields or index is None or index >= len(self.doc.objects):
            return
        obj = self.doc.objects[index]
        if name in ("background", "text") and name not in obj:
            return
        before = self.doc.state()
        obj[name] = value
        self.doc.record(before)

    def load_object_fields(self):
        self.loading_fields = True
        self.object_text.clear()
        index = self.canvas.selected
        if index is not None and index < len(self.doc.objects):
            obj = self.doc.objects[index]
            self.object_text.setPlainText(obj.get("text", ""))
            for field, name in [(self.line_width, "width"), (self.font_size, "font_size"),
                                (self.mosaic_strength, "strength")]:
                field.setValue(obj.get(name, field.value()))
            self.color = obj.get("color", self.color)
        rect = self.canvas.current_rect()
        if rect:
            for field, value in zip(self.rect_fields, rect_tuple(rect)):
                field.setValue(value)
        self.loading_fields = False
        self.update_context()
        self.update_status()

    def apply_rect_fields(self):
        rect = bounded_rect(QRectF(*[field.value() for field in self.rect_fields]), self.doc.bounds)
        current = self.canvas.current_rect()
        if rect is None or current is None:
            return
        if self.canvas.tool == "ocrbox" and self.request_active and self.request_kind != "translate":
            self.cancel_api()
        before = self.doc.state()
        self.canvas.original = current
        self.canvas.set_target_rect(rect)
        self.doc.record(before)
        self.load_object_fields()

    def delete_object(self):
        if self.canvas.tool == "ocrbox" and self.canvas.selected_block is not None:
            if self.ocr_revision != self.doc.revision or (self.request_active and self.request_kind != "translate"):
                self.api_error("当前识别框不可用，请完成提取后再删除。")
                return
            self.save_block_text()
            before = self.doc.state()
            block = self.blocks.pop(self.canvas.selected_block)
            self.doc.objects = [obj for obj in self.doc.objects
                                if not (obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session
                                        and obj.get("block_id") == block["id"])]
            self.refresh_blocks()
            self.doc.record(before)
            if not self.blocks and self.request_active:
                self.cancel_api()
            self.load_object_fields()
            self.status.setText("已移除该识别框及对应译文；原图文字保留，可撤销。")
            return
        index = self.canvas.selected
        if index is not None and index < len(self.doc.objects):
            before = self.doc.state()
            obj = self.doc.objects[index]
            block_changed = False
            if obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session:
                for block in self.blocks:
                    if block["id"] == obj.get("block_id"):
                        block["apply"] = False
                        block_changed = True
            del self.doc.objects[index]
            self.canvas.selected = None
            self.doc.record(before)
            if block_changed:
                self.refresh_blocks()
            self.load_object_fields()
        else:
            self.status.setText("先点击画布中的标注、译文或识别框，再删除。")

    def undo(self):
        self.canvas.selected = None
        self.doc.undo()
        self.load_object_fields()

    def redo(self):
        self.canvas.selected = None
        self.doc.redo()
        self.load_object_fields()

    def update_status(self):
        if hasattr(self, "status"):
            self.history_buttons["undo"].setEnabled(bool(self.doc.undo_states))
            self.history_buttons["redo"].setEnabled(bool(self.doc.redo_states))
            self.history_buttons["delete"].setEnabled(
                self.canvas.selected is not None or
                (self.canvas.tool == "ocrbox" and self.canvas.selected_block is not None
                 and self.ocr_revision == self.doc.revision))
            area = self.doc.export_rect()
            overflow = any(text_overflows(obj) for obj in self.doc.objects)
            zoom = (f" · 显示 {self.zoom_factor:.0%}" if self.zoom_factor else " · 适应窗口") \
                + " · Ctrl＋滚轮缩放" if not self.fullscreen else ""
            self.status.setText(f"{area.width()} × {area.height()} · {len(self.doc.objects)} 个图层" + zoom +
                                (" · 文字溢出：请扩大框 / 减小字号" if overflow else " · 完成后复制并保存"))

    def document_changed(self):
        source_valid = self.ocr_source_state is not None and \
            self.recognition_source_state() == self.ocr_source_state
        if self.applying_translation or source_valid:
            # 移动本轮译文/校对识别框不改变识别底图，文本翻译响应仍可使用最新位置。
            self.ocr_revision = self.doc.revision
        elif self.ocr_revision is not None and self.ocr_revision != self.doc.revision:
            self.ocr_revision = None
            self.block_list.setEnabled(False)
            self.panel_note.setText("图片已改变，旧文字位置失效，请重新提取。\n译文图层仍可用移动工具调整。")
        if self.request_active and self.request_revision != self.doc.revision:
            if self.request_kind == "translate" and source_valid:
                self.request_revision = self.doc.revision
            else:
                self.cancel_api()
        self.update_status()
        self.refresh_text_controls()
        self.canvas.update()

    def open_settings(self):
        if self.store:
            SettingsDialog(self.store, self).exec()
            self.language.setCurrentText(self.store.settings.language)

    def api_busy(self, busy):
        # 请求期间锁定其文字快照，画布仍可编辑/取消；避免返回结果覆盖正在校对的字。
        busy = busy or self.translation_session is not None
        for field in (self.source_text, self.translated_text):
            field.setReadOnly(busy)
        self.language.setEnabled(not busy)
        self.refresh_text_controls()
        if busy:
            message = self.translation_session.progress if self.translation_session is not None else \
                "正在本地识别…" if self.local_client.busy else \
                "正在翻译…" if getattr(self, "request_kind", "") == "translate" else "正在提取文字…"
            self.status.setText(message)
            self.panel_note.setText(message if self.translation_session is not None else message + " 可取消。")
        self.layout_panels()

    def cancel_api(self):
        self.request_revision = None
        self.translation_session = None
        self.translation_id_map = self.translation_originals = None
        self.pending_translation_preview = False
        self.client.cancel()
        self.local_client.cancel()
        self.api_busy(False)
        self.status.setText("请求已取消，编辑内容已保留。")
        self.panel_note.setText("已取消，可重新提取或继续编辑。")
        self.refresh_text_controls()

    def api_error(self, message):
        if self.translation_session is not None:
            session, self.translation_session = self.translation_session, None
            if session.results:
                pending = [block_id for block_id in session.blocks if block_id not in session.results]
                self.accept_translation_results(session.results, pending, message)
                return
            self.api_busy(False)
        self.status.setText(message)
        self.status.setToolTip(message)
        self.panel_note.setText(message)
        self.refresh_text_controls()
        self.layout_panels()

    def extract(self):
        if self.request_active:
            return
        if self.fullscreen and not self.doc.crop:
            self.api_error("请先框选需要提取的区域。")
            return
        self.request_revision = self.doc.revision
        self.translation_retry_ids.clear()
        self.translation_selection = None
        self.translation_id_map = self.translation_originals = None
        self.request_kind = "ocr"
        rect, image = self.doc.export_rect(), self.doc.render()
        self.request_offset, self.request_size = (rect.x(), rect.y()), (image.width(), image.height())
        self.client.cancel()
        self.local_client.cancel()
        if self.store is None or self.store.settings.ocr_provider == "local":
            self.local_client.request(image)
        else:
            self.client.request(self.store.settings, "ocr", OCR_PROMPT, image)

    def translate(self):
        if self.request_active:
            return
        if not self.store:
            self.api_error("请连接 API 配置。")
            return
        if self.ocr_revision != self.doc.revision:
            self.api_error("请先完成文字提取；图片已改变时点击重新提取。")
            return
        self.save_block_text()
        selected = [block for block in self.blocks if block["apply"]]
        if not selected:
            self.api_error("请勾选需要翻译的文字块。")
            return
        target = self.language.currentText().strip() or "简体中文"
        selection = (target, tuple((block["id"], block["text"]) for block in selected))
        if self.translation_retry_ids and selection == self.translation_selection:
            selected = [block for block in selected if block["id"] in self.translation_retry_ids]
        self.translation_selection = selection
        self.translation_notice = ""
        self.request_revision = self.doc.revision
        self.translation_ids = [block["id"] for block in selected]
        # 请求使用连续编号；本地识别 ID 可能因取消勾选/删除而不连续。
        # 将返回值按请求快照映射回稳定 ID，乱序返回和在途删块也不会错配。
        wire_blocks = [{"id": str(index + 1), "text": block["text"]}
                       for index, block in enumerate(selected)]
        self.translation_id_map = {wire["id"]: block["id"] for wire, block in zip(wire_blocks, selected)}
        self.translation_originals = {wire["id"]: wire["text"] for wire in wire_blocks}
        self.request_kind = "translate"
        self.translation_session = TranslationSession(wire_blocks, target)
        self.send_translation_batch()

    def send_translation_batch(self):
        session = self.translation_session
        prompt = session.next_prompt()
        if prompt is None:
            self.translation_session = None
            self.accept_translation_results(session.results, sorted(session.failed))
            return
        self.client.request(self.store.settings, "translate", prompt)
        if self.translation_session is session:
            self.api_busy(True)

    def accept_translation_results(self, parsed, pending=(), error=""):
        """先保存有效文字，再应用成功区域；漏译块保留原画面并显式标记。"""
        local_id = lambda block_id: self.translation_id_map[block_id] \
            if self.translation_id_map is not None else block_id
        translations = {local_id(block_id): text for block_id, text in parsed.items()}
        failed = {local_id(block_id) for block_id in pending}
        self.translation_retry_ids = failed
        self.translation_cache.update(translations)
        for block in self.blocks:
            if block["id"] in translations:
                block["translated"] = translations[block["id"]]
                block.pop("translation_error", None)
            elif block["id"] in failed:
                block["translated"] = ""
                block["translation_error"] = "接口未返回有效译文，原画面已保留；可重试或手动填写。"
        self.translation_notice = (
            f"本次已翻译 {len(translations)} 块，{len(failed)} 块未完成，未完成区域保留原画面。"
            "点击“重试未完成”，或选中标记的块手动填写译文。"
            + (f"\n{error}" if error else "")) if failed else ""
        self.api_busy(False)
        if self.canvas.drag is not None:
            # 自动补译也允许拖动；最终响应只更新文字，松开后按最新位置生成图层。
            self.pending_translation_preview = True
            if self.canvas.before and len(self.canvas.before) == 3:
                for block in self.canvas.before[2]["blocks"]:
                    if block["id"] in translations:
                        block["translated"] = translations[block["id"]]
                        block.pop("translation_error", None)
                    elif block["id"] in failed:
                        block["translated"] = ""
                        block["translation_error"] = "接口未返回有效译文，原画面已保留。"
            self.load_block_fields()
            self.status.setText("补译结束，松开鼠标后更新图片预览。" if failed else
                                "翻译已完成，松开鼠标后更新图片预览。")
            self.refresh_text_controls()
            return
        self.refresh_blocks()
        self.show_panel()
        self.complete_translation_preview()

    def complete_translation_preview(self):
        selected = [block for block in self.blocks if block["apply"] and block["translated"].strip()]
        if selected and all(block.get("rect") for block in selected):
            self.apply_translations(allow_partial=bool(self.translation_retry_ids))
            message = "译文已替换到图片预览。修改勾选 / 译文后点应用更新；完成才复制保存。"
        elif selected:
            message = "翻译完成。请补充缺失的位置框，再点击应用译文。"
        else:
            message = "接口仍未返回有效译文，原图未修改。可重试未完成项、手动填写译文或更换模型。"
        self.status.setText(self.translation_notice or message)
        self.status.setToolTip(self.translation_notice or message)
        if self.translation_notice:
            self.panel_note.setText(self.translation_notice)
        self.refresh_text_controls()
        self.layout_panels()

    def api_result(self, kind, data):
        if self.request_revision != self.doc.revision or self.finished:
            return
        try:
            if kind == "ocr":
                self.blocks = parse_ocr(data, self.request_size, self.request_offset)
                self.ocr_session += 1
                self.translation_cache = {}
                self.translation_retry_ids.clear()
                self.translation_selection = None
                self.ocr_source_state = deepcopy(self.recognition_source_state())
                self.translation_source = None
                self.ocr_revision = self.doc.revision
                self.refresh_blocks()
                self.show_panel()
                self.set_tool("ocrbox")
                self.panel_note.setText(f"已提取 {len(self.blocks)} 处文字，翻译由你决定。" if self.blocks
                                        else "没有识别到文字，可调整选区后重新提取。")
                label = f"本地识别 {data['elapsed']:.2f} 秒" if data.get("engine") == "RapidOCR" else "提取完成"
                self.status.setText(f"{label} · {len(self.blocks)} 个文字块。可校对后翻译。")
            else:
                if self.translation_session is not None:
                    self.translation_session.accept(data)
                    self.send_translation_batch()
                    return
                ids = list(self.translation_id_map) if self.translation_id_map is not None else self.translation_ids
                parsed = parse_translations(data, ids, self.translation_originals)
                self.accept_translation_results(parsed)
        except ValueError as error:
            self.api_error(str(error))
        self.refresh_text_controls()

    def refresh_blocks(self):
        self.rebuilding_block_list = True
        selected = self.canvas.selected_block
        selected_id = self.blocks[selected]["id"] if selected is not None and selected < len(self.blocks) else None
        self.canvas.selected_block = None
        self.block_list.blockSignals(True)
        self.block_list.clear()
        for block in self.blocks:
            failed = block.get("translation_error") and not block["translated"].strip()
            item = QListWidgetItem(("⚠ 未完成 · " if failed else "") + block["text"][:60])
            if failed:
                item.setToolTip(block["translation_error"])
                item.setForeground(QColor("#b45309"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if block["apply"] else Qt.CheckState.Unchecked)
            self.block_list.addItem(item)
        self.block_list.setEnabled(True)
        self.block_list.blockSignals(False)
        self.block_list.setFixedHeight(min(96, max(42, len(self.blocks)*30+6)))
        if self.blocks:
            row = next((i for i, block in enumerate(self.blocks) if block["id"] == selected_id), 0)
            self.block_list.setCurrentRow(row)
            self.select_block(row)
        else:
            self.loading_fields = True
            self.source_text.clear()
            self.translated_text.clear()
            self.loading_fields = False
        self.refresh_text_controls()
        self.rebuilding_block_list = False

    def select_block(self, index):
        # 切换块前保存正在输入的校对内容，避免选择另一行后丢失。
        self.save_block_text()
        self.canvas.selected_block = index if 0 <= index < len(self.blocks) else None
        self.load_block_fields()
        if self.canvas.selected_block is not None and self.canvas.tool != "ocrbox" and not self.rebuilding_block_list:
            block_id = self.blocks[self.canvas.selected_block]["id"]
            layer = next((i for i, obj in enumerate(self.doc.objects)
                          if obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session
                          and obj.get("block_id") == block_id), None)
            self.canvas.selected = layer
            self.set_tool("select" if layer is not None else "ocrbox")
            self.load_object_fields()
        self.update_status()
        self.canvas.update()

    def block_checked(self, item):
        index = self.block_list.row(item)
        if 0 <= index < len(self.blocks):
            self.blocks[index]["apply"] = item.checkState() == Qt.CheckState.Checked
        self.refresh_text_controls()

    def load_block_fields(self):
        index = self.canvas.selected_block
        if index is not None and index < len(self.blocks):
            block = self.blocks[index]
            # 两个输入框一起加载时不能把尚未更新的另一个框反写到新结果中。
            previous_loading = self.loading_fields
            self.loading_fields = True
            self.source_text.setPlainText(block["text"])
            self.translated_text.setPlainText(block["translated"])
            if block.get("rect") and self.canvas.tool == "ocrbox":
                for field, value in zip(self.rect_fields, block["rect"]):
                    field.setValue(value)
            self.loading_fields = previous_loading

    def save_block_text(self):
        index = self.canvas.selected_block
        if index is not None and index < len(self.blocks):
            self.blocks[index]["text"] = self.source_text.toPlainText()
            self.blocks[index]["translated"] = self.translated_text.toPlainText()
            block = self.blocks[index]
            if block["translated"].strip():
                block.pop("translation_error", None)
                self.translation_retry_ids.discard(block["id"])
            if index < self.block_list.count():
                item = self.block_list.item(index)
                failed = block.get("translation_error") and not block["translated"].strip()
                item.setText(("⚠ 未完成 · " if failed else "") + block["text"][:60])
                item.setForeground(QColor("#b45309" if failed else "#202633"))
                item.setToolTip(block.get("translation_error", "") if failed else "")

    def copy_ocr(self):
        self.save_block_text()
        if self.ocr_revision != self.doc.revision:
            self.api_error("图片已改变，请重新提取后复制。")
            return
        text = "\n".join(block["text"] for block in self.blocks if block["apply"])
        if text:
            self.text_copied.emit(text)
            self.status.setText("所选原文已复制。")

    def apply_translations(self, *, allow_partial=False):
        if self.request_active:
            self.api_error("请等待翻译完成，或先取消请求。")
            return
        self.save_block_text()
        if self.ocr_revision != self.doc.revision:
            self.api_error("图片已改变，请重新提取；已有译文图层可直接调整。")
            return
        selected = [block for block in self.blocks if block["apply"]]
        allow_partial = allow_partial or any(
            b.get("translation_error") and not b["translated"].strip() for b in selected)
        preserve = {b["id"] for b in selected if not b["translated"].strip()} if allow_partial else set()
        if allow_partial:
            selected = [b for b in selected if b["id"] not in preserve]
        if any(not b["translated"].strip() or not b.get("rect") for b in selected):
            self.api_error("所选块需要译文和位置框。缺少位置时选中块，点击调整文字框后拖出范围。")
            return
        if self.translation_source is None:
            self.translation_source = self.doc.full_image().copy()
        current, objects = self.translation_source, []
        active = self.canvas.selected_block
        active_id = self.blocks[active]["id"] if active is not None and active < len(self.blocks) else None
        existing = {obj.get("block_id"): obj for obj in self.doc.objects
                    if obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session}
        for block in selected:
            rect = rect_of(block["rect"]).intersected(rect_of(self.doc.crop) if self.doc.crop else self.doc.bounds)
            if rect.isEmpty():
                continue
            # 视觉模型框可能切到字形边缘；小幅覆盖余量去除残笔，用户仍可单独校对。
            crop = rect_of(self.doc.crop) if self.doc.crop else self.doc.bounds
            padding = max(3, min(8, rect.height() * .18))
            coverage = rect.adjusted(-3, -padding, 3, padding).intersected(crop)
            background = estimate_background(current, coverage)
            foreground = estimate_foreground(current, rect, background)
            size = fit_font(block["translated"], rect, min(48, max(12, rect.height() * .75)))
            previous = existing.get(block["id"])
            if previous and previous.get("block_rect") == block["rect"]:
                # 校对译文后更新预览，保留用户已经调好的排版与覆盖框。
                obj = deepcopy(previous)
                obj["text"] = block["translated"]
            else:
                obj = {"kind": "translation", "rect": rect_tuple(rect), "source_rect": rect_tuple(coverage),
                       "text": block["translated"], "font_size": size, "color": foreground,
                       "background": background, "block_id": block["id"], "ocr_session": self.ocr_session,
                       "block_rect": block["rect"]}
            objects.append(obj)
        block_ids = {block["id"] for block in self.blocks}
        before = self.doc.state()
        self.doc.objects = [obj for obj in self.doc.objects
                            if not (obj["kind"] == "translation" and obj.get("block_id") in block_ids
                                    and obj.get("block_id") not in preserve
                                    and obj.get("ocr_session") == self.ocr_session)]
        self.doc.objects.extend(objects)
        self.applying_translation = True
        try:
            self.doc.record(before)
        finally:
            self.applying_translation = False
        if objects:
            self.canvas.selected = next((i for i, obj in enumerate(self.doc.objects)
                if obj["kind"] == "translation" and obj.get("ocr_session") == self.ocr_session
                and obj.get("block_id") == active_id), len(self.doc.objects)-1)
            self.set_tool("select")
            self.load_object_fields()
            self.show_panel()
            self.panel_note.setText("译文已替换到预览，可调整、改字、配色。\n"
                                    "改勾选/译文后点应用更新；Ctrl+Z 恢复原文。\n点击完成才复制保存。")
            if preserve and self.translation_notice:
                self.panel_note.setText(self.translation_notice)
        else:
            self.canvas.selected = None
            self.load_object_fields()

    def commit(self):
        if self.finished or (self.fullscreen and not self.doc.crop):
            return
        if any(text_overflows(obj) for obj in self.doc.objects):
            self.api_error("文字超出文本框，请扩大框、减小字号或修改内容后完成。")
            self.canvas.selected = next(i for i, obj in enumerate(self.doc.objects) if text_overflows(obj))
            self.set_tool("select")
            self.show_properties()
            return
        image = self.doc.render()
        update = self.update_handler is not None and self.save_mode.currentData() == "update"
        if update:
            import sqlite3
            try:
                self.update_handler(image)
            except ValueError as error:
                self.api_error(str(error))
                return
            except (sqlite3.Error, OSError, RuntimeError):
                self.api_error("修改未保存，请检查数据目录与磁盘空间。编辑内容已保留。")
                return
        self.finished = True
        self.client.cancel()
        self.local_client.cancel()
        self.hide()
        if not update:
            self.selected.emit(image)
        self.close()

    def save_png(self):
        if self.fullscreen and not self.doc.crop:
            self.api_error("请先框选需要保存的区域。")
            return
        if any(text_overflows(obj) for obj in self.doc.objects):
            self.api_error("文字超出文本框，请先调整后保存。")
            return
        directory = Path(getattr(self.store, "export_dir", Path(__file__).parent / "data" / "exports"))
        name = directory / f"剪贴收藏-{time_ns()}.png"
        filename, _ = QFileDialog.getSaveFileName(self, "另存为 PNG", str(name), "PNG 图片 (*.png)")
        if not filename:
            return
        target = Path(filename)
        if target.suffix.lower() != ".png":
            target = Path(str(target) + ".png")
            if target.exists():
                self.api_error("补上 PNG 扩展名后文件已存在，请选择另一个文件名。")
                return
        try:
            write_png(self.doc.render(), target)
        except OSError:
            self.api_error("图片未保存，请检查文件夹权限和磁盘空间。")
            return
        self.status.setText("PNG 已保存，仍可继续编辑。")

    def cancel(self):
        if not self.finished:
            self.finished = True
            self.client.cancel()
            self.local_client.cancel()
            self.hide()
            self.cancelled.emit()
            self.close()

    def reject(self):
        self.cancel()

    def closeEvent(self, event):
        self.close_full_text()
        if not self.finished:
            self.finished = True
            self.client.cancel()
            self.local_client.cancel()
            self.cancelled.emit()
        event.accept()

    @property
    def request_active(self):
        return self.translation_session is not None or self.client.reply is not None or self.local_client.busy
