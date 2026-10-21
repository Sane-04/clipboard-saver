"""日期查询与剪贴历史窗口，界面不直接写入剪贴板或数据库。"""
from collections import OrderedDict
from datetime import date, datetime, timedelta

from PySide6.QtCore import QAbstractListModel, QDate, QItemSelectionModel, QModelIndex, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)
from ui_theme import ACCENT, INK, MUTED, STYLE, icon
from shortcut_settings import DEFAULT_SHORTCUTS
from window_pin import TopmostButton


def application_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)
    painter.setPen(QPen(QColor("white"), 3.5))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(19, 17, 30, 34), 5, 5)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(QRectF(12, 24, 30, 30), 5, 5)
    painter.drawLine(19, 34, 34, 34)
    painter.drawLine(19, 42, 29, 42)
    painter.end()
    return QIcon(pixmap)


def display_time(timestamp_ns):
    return datetime.fromtimestamp(timestamp_ns // 1_000_000_000).strftime("%Y-%m-%d %H:%M")


def paint_checkerboard(painter, rect, tile=12):
    painter.fillRect(rect, QColor("#ffffff"))
    painter.save()
    painter.setClipRect(rect)
    left, top = int(rect.left()), int(rect.top())
    for y in range(top, int(rect.bottom()) + tile, tile):
        for x in range(left, int(rect.right()) + tile, tile):
            if ((x - left) // tile + (y - top) // tile) % 2:
                painter.fillRect(x, y, tile, tile, QColor("#eceef3"))
    painter.restore()


class ImagePreview(QWidget):
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.setMinimumSize(180, 150)

    def set_image(self, image):
        self.image = image
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.image.isNull():
            self.double_clicked.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f3f5f8"))
        if self.image.isNull():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        bounds = self.rect().adjusted(20, 20, -20, -20)
        size = self.image.size().scaled(bounds.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRectF(bounds.center().x() - size.width() / 2,
                        bounds.center().y() - size.height() / 2, size.width(), size.height())
        paint_checkerboard(painter, target)
        painter.drawImage(target, self.image)


class DateRangeDialog(QDialog):
    def __init__(self, start=None, end=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择查询日期")
        self.setStyleSheet(STYLE)
        self.resize(390, 245)
        self.range = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        self.mode = QComboBox()
        self.mode.addItems(["指定日期", "日期范围", "全部日期"])
        self.mode.setCurrentIndex(2 if not start and not end else 1 if start and end and start != end else 0)
        self.start = QDateEdit(QDate.fromString(start, "yyyy-MM-dd") if start else QDate.currentDate())
        self.end = QDateEdit(QDate.fromString(end, "yyyy-MM-dd") if end else self.start.date())
        for field in (self.start, self.end):
            field.setCalendarPopup(True)
            field.setDisplayFormat("yyyy-MM-dd")
        form = QFormLayout()
        form.setSpacing(12)
        form.addRow("查询方式", self.mode)
        self.start_label = QLabel("日期 / 开始")
        form.addRow(self.start_label, self.start)
        self.end_label = QLabel("结束日期")
        form.addRow(self.end_label, self.end)
        layout.addLayout(form)
        self.error = QLabel("结束日期不能早于开始日期。")
        self.error.setObjectName("error")
        self.error.hide()
        layout.addWidget(self.error)
        self.mode.currentIndexChanged.connect(self.toggle_range)
        self.toggle_range()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("查询")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def toggle_range(self, *_):
        multiple = self.mode.currentIndex() == 1
        self.start.setVisible(self.mode.currentIndex() != 2)
        self.start_label.setVisible(self.mode.currentIndex() != 2)
        self.end.setVisible(multiple)
        self.end_label.setVisible(multiple)
        self.error.hide()

    def apply(self):
        if self.mode.currentIndex() == 2:
            self.range = (None, None)
            self.accept()
            return
        finish = self.end.date() if self.mode.currentIndex() == 1 else self.start.date()
        if finish < self.start.date():
            self.error.show()
            return
        self.range = (self.start.date().toString("yyyy-MM-dd"), finish.toString("yyyy-MM-dd"))
        self.accept()


class HistoryModel(QAbstractListModel):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage, self.rows = storage, []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def flags(self, index):
        # 日期标题不参与选择、复制或删除，键盘导航同样跳过它。
        if not index.isValid() or self.rows[index.row()].get("header"):
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.DisplayRole:
            return row["copy_day"] if row.get("header") else row["preview"] if row["kind"] == "text" else "图片"
        return None

    def reload(self, search, kind, start=None, end=None, marked_only=False):
        records = self.storage.list_records(search, kind, start, end, marked_only)
        self.beginResetModel()
        self.rows = []
        previous = None
        for record in records:
            if record["copy_day"] != previous:
                previous = record["copy_day"]
                self.rows.append(dict(header=True, copy_day=previous))
            self.rows.append(record)
        self.endResetModel()


class HistoryDelegate(QStyledItemDelegate):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.thumbnails = OrderedDict()

    def sizeHint(self, option, index):
        return QSize(200, 32 if index.data(Qt.ItemDataRole.UserRole).get("header") else 86)

    def thumbnail(self, record_id):
        if record_id not in self.thumbnails:
            # 原图只在选择时读取，可见行缩略图最多缓存 64 张。
            self.thumbnails[record_id] = QImage.fromData(self.storage.thumbnail(record_id))
            if len(self.thumbnails) > 64:
                self.thumbnails.popitem(last=False)
        self.thumbnails.move_to_end(record_id)
        return self.thumbnails[record_id]

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        if row.get("header"):
            painter.setPen(QColor(MUTED))
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.drawText(rect.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, row["copy_day"])
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#edf1ff" if selected else "#f1f3f7" if hovered else "#ffffff"))
        painter.drawRoundedRect(rect, 8, 8)
        if selected:
            painter.fillRect(QRectF(rect.left(), rect.top() + 12, 3, rect.height() - 24), QColor(ACCENT))
        content = rect.adjusted(13, 11, -13, -10)
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.setPen(QColor(MUTED))
        painter.drawText(content, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                         display_time(row["display_at"])[11:])
        left = content.left()
        if row["kind"] == "image":
            thumb = self.thumbnail(row["id"])
            box = QRectF(left, content.top() + 3, 64, 48)
            paint_checkerboard(painter, box, 6)
            if not thumb.isNull():
                size = thumb.size().scaled(64, 48, Qt.AspectRatioMode.KeepAspectRatio)
                painter.drawImage(QRectF(box.center().x() - size.width()/2,
                                        box.center().y() - size.height()/2, size.width(), size.height()), thumb)
            left += 78
            title, subtitle = "图片", f'{row["width"]} × {row["height"]}'
        else:
            lines = "  ".join(row["preview"].replace("\r", "").split("\n"))
            title, subtitle = lines[:60], "文字" + (" · 已固定" if row["pinned"] else "")
        body = QRectF(left, content.top(), max(1, content.right() - left - 53), 26)
        painter.setPen(QColor(INK))
        painter.setFont(QFont("Microsoft YaHei UI", 10))
        painter.drawText(body, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(body.width())))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.setPen(QColor(MUTED))
        if row["marked"]:
            subtitle += " · ★ " + (row["tag"] or "特殊标记")
        subtitle_rect = QRectF(left, content.top() + 31, max(1, content.right() - left), 21)
        painter.drawText(subtitle_rect, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight,
                                                         int(subtitle_rect.width())))
        if row["pinned"] and row["kind"] == "image":
            painter.drawText(content, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, "已固定")
        painter.restore()


class HistoryWindow(QMainWindow):
    copy_requested = Signal(int)
    pin_requested = Signal(int)
    delete_requested = Signal(int)
    delete_many_requested = Signal(object)
    clear_requested = Signal()
    screenshot_requested = Signal()
    long_screenshot_requested = Signal()
    pause_requested = Signal()
    settings_requested = Signal()
    shortcuts_requested = Signal()
    history_settings_requested = Signal()
    date_cleanup_requested = Signal()
    locations_requested = Signal()
    autostart_requested = Signal(bool)
    mark_requested = Signal(int)
    image_edit_requested = Signal(str)
    text_edit_requested = Signal(int)
    hidden = Signal()

    def __init__(self, storage, shortcut_store=None):
        super().__init__()
        self.storage = storage
        self.shortcut_store = shortcut_store
        self.current_id = self.current_key = None
        self.current_image = QImage()
        self.exiting = False
        self.image_viewers = []
        self.kind, self.date_mode = "", "all"
        self.marked_only = False
        self.date_start = self.date_end = None
        self.today = date.today()
        self.setWindowTitle("剪贴收藏")
        self.setWindowIcon(application_icon())
        self.setStyleSheet(STYLE)
        self.setMinimumSize(740, 490)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1080, available.width() - 40), min(720, available.height() - 70))
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 17, 20, 15)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("所有记录")
        title.setObjectName("heading")
        heading.addWidget(title)
        heading.addStretch()
        self.status = QLabel("● 正在记录")
        self.status.setObjectName("status")
        heading.addWidget(self.status)
        self.screenshot_button = QPushButton()
        self.screenshot_button.clicked.connect(self.screenshot_requested)
        heading.addWidget(self.screenshot_button)
        self.topmost_button = TopmostButton(self)
        heading.addWidget(self.topmost_button)
        more = QPushButton()
        more.setIcon(icon("more"))
        more.setToolTip("设置与更多操作")
        menu = QMenu(self)
        settings = menu.addAction("文字识别与 API 设置")
        settings.triggered.connect(self.settings_requested)
        shortcuts = menu.addAction("快捷键设置")
        shortcuts.triggered.connect(self.shortcuts_requested)
        history_settings = menu.addAction("历史保留设置")
        history_settings.triggered.connect(self.history_settings_requested)
        locations = menu.addAction("数据保存位置")
        locations.triggered.connect(self.locations_requested)
        self.long_screenshot_action = menu.addAction("滚动长截图")
        self.long_screenshot_action.triggered.connect(self.long_screenshot_requested)
        self.autostart_action = menu.addAction("登录 Windows 后自动启动")
        self.autostart_action.setCheckable(True)
        self.autostart_action.triggered.connect(self.autostart_requested)
        self.pause_action = menu.addAction("暂停记录")
        self.pause_action.triggered.connect(self.pause_requested)
        menu.addSeparator()
        self.date_cleanup_action = menu.addAction("删除指定日期以前的数据…")
        self.date_cleanup_action.triggered.connect(self.date_cleanup_requested)
        self.clear_action = menu.addAction("清空全部日期的普通记录…")
        self.clear_action.triggered.connect(self.confirm_clear)
        more.setMenu(menu)
        heading.addWidget(more)
        layout.addLayout(heading)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文字或特殊标记标签")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        search_row.addWidget(self.search, 1)
        self.date_button = QPushButton("全部日期")
        self.date_button.setIcon(icon("calendar", ACCENT))
        self.date_button.setMinimumWidth(213)
        self.date_button.clicked.connect(self.open_dates)
        search_row.addWidget(self.date_button)
        layout.addLayout(search_row)
        filters = QHBoxLayout()
        self.category_group = QButtonGroup(self)
        self.category_buttons = {}
        for label, kind in [("全部", ""), ("文字", "text"), ("图片", "image")]:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setObjectName("quiet")
            button.clicked.connect(lambda checked=False, value=kind: self.set_kind(value))
            self.category_group.addButton(button)
            self.category_buttons[kind] = button
            filters.addWidget(button)
        self.category_buttons[""].setChecked(True)
        self.marked_filter = QPushButton("仅特殊标记")
        self.marked_filter.setIcon(icon("star"))
        self.marked_filter.setCheckable(True)
        self.marked_filter.setObjectName("quiet")
        self.marked_filter.toggled.connect(self.set_marked_only)
        filters.addWidget(self.marked_filter)
        filters.addStretch()
        self.date_group = QButtonGroup(self)
        self.date_buttons = {}
        for label, mode in [("全部日期", "all"), ("今天", "today"), ("昨天", "yesterday"), ("最近7天", "week")]:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setObjectName("quiet")
            button.clicked.connect(lambda checked=False, value=mode: self.set_date_mode(value))
            self.date_group.addButton(button)
            self.date_buttons[mode] = button
            filters.addWidget(button)
        self.date_buttons["all"].setChecked(True)
        layout.addLayout(filters)
        splitter = QSplitter()
        self.model = HistoryModel(storage, self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.delegate = HistoryDelegate(storage, self.list_view)
        self.list_view.setItemDelegate(self.delegate)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self.open_record_menu)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMouseTracking(True)
        self.list_view.setMinimumWidth(240)
        self.list_view.selectionModel().currentChanged.connect(self.select_record)
        self.list_view.doubleClicked.connect(self.activate_record)
        splitter.addWidget(self.list_view)
        preview = QFrame()
        preview.setObjectName("preview")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(16, 13, 16, 14)
        meta_row = QHBoxLayout()
        self.metadata = QLabel("内容预览")
        self.metadata.setObjectName("muted")
        meta_row.addWidget(self.metadata, 1)
        self.edit_content_button = QPushButton("编辑内容")
        self.edit_content_button.setObjectName("quiet")
        self.edit_content_button.clicked.connect(
            lambda: self.text_edit_requested.emit(self.current_id) if self.current_id is not None else None)
        self.edit_content_button.hide()
        meta_row.addWidget(self.edit_content_button)
        self.pin_button = QPushButton("固定")
        self.pin_button.setObjectName("quiet")
        self.pin_button.clicked.connect(lambda: self.pin_requested.emit(self.current_id) if self.current_id else None)
        meta_row.addWidget(self.pin_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("quiet")
        self.delete_button.clicked.connect(self.delete_selected)
        meta_row.addWidget(self.delete_button)
        preview_layout.addLayout(meta_row)
        mark_row = QHBoxLayout()
        self.mark_button = QPushButton("特殊标记")
        self.mark_button.setIcon(icon("star"))
        self.mark_button.setObjectName("quiet")
        self.mark_button.clicked.connect(
            lambda: self.mark_requested.emit(self.current_id) if self.current_id is not None else None)
        mark_row.addWidget(self.mark_button)
        self.tag_label = QLabel()
        self.tag_label.setObjectName("muted")
        self.tag_label.setWordWrap(True)
        self.tag_label.setTextFormat(Qt.TextFormat.PlainText)
        mark_row.addWidget(self.tag_label, 1)
        preview_layout.addLayout(mark_row)
        self.preview_stack = QStackedWidget()
        self.empty_label = QLabel()
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.preview_stack.addWidget(self.empty_label)
        self.text_preview = QPlainTextEdit()
        self.text_preview.setObjectName("textPreview")
        self.text_preview.setReadOnly(True)
        self.preview_stack.addWidget(self.text_preview)
        self.image_preview = ImagePreview()
        self.image_preview.setToolTip("双击图片，在独立窗口中查看")
        self.image_preview.double_clicked.connect(self.open_image_viewer)
        self.preview_stack.addWidget(self.image_preview)
        preview_layout.addWidget(self.preview_stack, 1)
        image_actions = QHBoxLayout()
        self.image_buttons = []
        for label, mode in [("编辑图片", "edit"), ("文本提取与翻译", "text")]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, action=mode: self.image_edit_requested.emit(action))
            button.hide()
            self.image_buttons.append(button)
            image_actions.addWidget(button)
        image_actions.addStretch()
        self.original_button = QPushButton("原尺寸 ↗")
        self.original_button.setObjectName("quiet")
        self.original_button.clicked.connect(self.open_original)
        self.original_button.hide()
        image_actions.addWidget(self.original_button)
        preview_layout.addLayout(image_actions)
        splitter.addWidget(preview)
        splitter.setSizes([330, 690])
        layout.addWidget(splitter, 1)
        footer = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        self.count_label.setWordWrap(True)
        footer.addWidget(self.count_label, 1)
        self.shortcut_hint = QLabel()
        self.shortcut_hint.setObjectName("muted")
        footer.addWidget(self.shortcut_hint)
        self.copy_button = QPushButton("复制并收起  Enter")
        self.copy_button.setObjectName("primary")
        self.copy_button.clicked.connect(self.copy_current)
        footer.addWidget(self.copy_button)
        layout.addLayout(footer)
        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.shortcuts = []
        for key, callback in [("Return", self.copy_current), ("Enter", self.copy_current),
                              ("Escape", self.hide_window), ("Ctrl+F", lambda: self.search.setFocus())]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        for key, step in [("Down", 1), ("Up", -1)]:
            shortcut = QShortcut(QKeySequence(key), self.list_view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda value=step: self.move_selection(value))
            self.shortcuts.append(shortcut)
        delete = QShortcut(QKeySequence("Delete"), self.list_view)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self.delete_selected)
        self.shortcuts.append(delete)
        self.list_view.selectionModel().selectionChanged.connect(self.update_delete_controls)
        if self.shortcut_store:
            self.shortcut_store.changed.connect(self.update_shortcut_labels)
        self.update_shortcut_labels()
        # 用本地日历检查跨午夜，也兼容电脑休眠后恢复；不按固定 24 小时推进日期。
        self.day_timer = QTimer(self)
        self.day_timer.setInterval(15000)
        self.day_timer.timeout.connect(self.check_day)
        self.day_timer.start()
        self.refresh()

    def set_kind(self, kind):
        self.kind = kind
        self.category_buttons[kind].setChecked(True)
        self.refresh()

    def set_marked_only(self, enabled):
        self.marked_only = enabled
        self.refresh()

    def set_date_mode(self, mode, start=None, end=None):
        if mode == "custom" and (not start or not end or end < start):
            raise ValueError("结束日期不能早于开始日期。")
        self.date_mode = mode
        today = date.today()
        if mode == "today":
            start = end = today.isoformat()
        elif mode == "yesterday":
            start = end = (today - timedelta(days=1)).isoformat()
        elif mode == "week":
            start, end = (today - timedelta(days=6)).isoformat(), today.isoformat()
        elif mode == "all":
            start = end = None
        self.date_start, self.date_end = start, end
        self.date_group.setExclusive(False)
        for key, button in self.date_buttons.items():
            button.setChecked(key == mode)
        self.date_group.setExclusive(True)
        label = "全部日期" if not start else start if start == end else f"{start} — {end}"
        self.date_button.setText(label)
        self.date_button.setToolTip("选择全部日期、指定日期或日期范围")
        self.refresh()

    def open_dates(self):
        dialog = DateRangeDialog(self.date_start, self.date_end, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_date_mode("all" if dialog.range == (None, None) else "custom", *dialog.range)

    def check_day(self):
        today = date.today()
        if today != self.today:
            self.today = today
            if self.date_mode in ("today", "yesterday", "week"):
                self.set_date_mode(self.date_mode)
            else:
                self.refresh()

    def refresh(self, *_args, select_id=None):
        old_key = self.current_key
        old_rows = [row for row in self.model.rows if not row.get("header")]
        old_ordinal = next((i for i, row in enumerate(old_rows)
                            if (row["id"], row["copy_day"]) == old_key), 0)
        old_selected = [(index.data(Qt.ItemDataRole.UserRole)["id"],
                         index.data(Qt.ItemDataRole.UserRole)["copy_day"])
                        for index in self.list_view.selectionModel().selectedIndexes()
                        if index.data(Qt.ItemDataRole.UserRole) and not index.data(Qt.ItemDataRole.UserRole).get("header")]
        selected = self.current_id if len(old_selected) > 1 else select_id if select_id is not None else self.current_id
        scroll = self.list_view.verticalScrollBar().value()
        # SQLite 删除末行后可能复用 ID；刷新时失效缓存，不能显示前一张图的缩略图。
        self.delegate.thumbnails.clear()
        self.model.reload(self.search.text(), self.kind, self.date_start, self.date_end, self.marked_only)
        records = [(i, row) for i, row in enumerate(self.model.rows) if not row.get("header")]
        counts = self.storage.counts()
        self.count_label.setText(f'{len(records)} 条结果 · 共 {counts["total"]} 项内容')
        ordinary = self.storage.ordinary_count()
        self.count_label.setToolTip(f'{counts["images"]} 张原图 · {counts["pinned"]} 项固定 · '
                                   f'{self.storage.marked_count()} 项特殊标记\n'
                                   f'普通记录 {ordinary} / {self.storage.limit}；同内容跨日不重复保存原图')
        # 内容 ID 在多个日期出现时，以 (ID, 日期) 恢复选择，避免跳到同内容的另一日。
        candidate = next((i for i, r in records if (r["id"], r["copy_day"]) == old_key and r["id"] == selected), None)
        if candidate is None:
            candidate = next((i for i, r in records if r["id"] == selected), None)
        if candidate is None:
            candidate = next((i for i, r in records if (r["id"], r["copy_day"]) == old_key), None)
        if candidate is None and records:
            # 删除后留在相邻内容，不突然跳回第一条；后台刷新仍保留用户的多选。
            candidate = records[min(old_ordinal, len(records)-1)][0]
        if candidate is not None:
            self.list_view.setCurrentIndex(self.model.index(candidate, 0))
            if len(old_selected) > 1:
                selection = self.list_view.selectionModel()
                selection.clearSelection()
                for row_index, row in records:
                    if (row["id"], row["copy_day"]) in old_selected:
                        selection.select(self.model.index(row_index, 0), QItemSelectionModel.SelectionFlag.Select)
                if not selection.hasSelection():
                    selection.select(self.model.index(candidate, 0), QItemSelectionModel.SelectionFlag.Select)
            self.list_view.verticalScrollBar().setValue(scroll)
        else:
            self.select_record(QModelIndex())
        self.clear_action.setEnabled(ordinary > 0)
        self.update_delete_controls()

    def select_record(self, index, _previous=QModelIndex()):
        row = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        has_record = bool(row and not row.get("header"))
        for button in (self.copy_button, self.pin_button, self.delete_button, self.mark_button):
            button.setEnabled(has_record)
        self.original_button.hide()
        self.edit_content_button.hide()
        for button in self.image_buttons:
            button.hide()
        self.current_image = QImage()
        if not has_record:
            self.current_id = self.current_key = None
            self.metadata.setText("内容预览")
            self.tag_label.clear()
            self.mark_button.setText("特殊标记")
            filtered = bool(self.search.text() or self.kind or self.date_start or self.marked_only)
            values = self.shortcut_store.bindings if self.shortcut_store else DEFAULT_SHORTCUTS
            self.empty_label.setText("该条件下没有匹配记录\n\n试试其他日期、关键词或内容分类。" if filtered else
                                     f'复制过的内容会留在这里\n\n复制文字或图片，或者按 {values["screenshot"]} 截图。')
            self.preview_stack.setCurrentIndex(0)
            return
        self.current_id = row["id"]
        self.current_key = (row["id"], row["copy_day"])
        record = self.storage.get_record(self.current_id)
        self.pin_button.setText("取消固定" if row["pinned"] else "固定")
        self.mark_button.setText("编辑标记" if record["marked"] else "特殊标记")
        self.mark_button.setIcon(icon("star", ACCENT if record["marked"] else MUTED))
        self.tag_label.setText(f'★ {record["tag"] or "已特殊标记"}' if record["marked"] else "")
        meta = f'{row["copy_day"]} {display_time(row["display_at"])[11:]}'
        if row["kind"] == "text":
            self.edit_content_button.show()
            self.text_preview.setPlainText(record["text"])
            self.preview_stack.setCurrentIndex(1)
            meta = f'文字 · {len(record["text"])} 字符\n{meta}'
        else:
            self.current_image = QImage.fromData(record["image"])
            self.image_preview.set_image(self.current_image)
            self.preview_stack.setCurrentIndex(2)
            self.original_button.show()
            for button in self.image_buttons:
                button.show()
            meta = f'图片 · {row["width"]} × {row["height"]}\n{meta}'
        self.metadata.setText(meta)

    def copy_current(self):
        if self.current_id is not None:
            self.copy_requested.emit(self.current_id)

    def activate_record(self, index):
        row = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        if not row or row.get("header"):
            return
        self.list_view.setCurrentIndex(index)
        if row["kind"] == "image":
            self.open_image_viewer()
        else:
            self.copy_current()

    def selected_record_ids(self):
        ids = [index.data(Qt.ItemDataRole.UserRole)["id"]
               for index in self.list_view.selectionModel().selectedIndexes()
               if index.data(Qt.ItemDataRole.UserRole) and not index.data(Qt.ItemDataRole.UserRole).get("header")]
        return list(dict.fromkeys(ids)) or ([self.current_id] if self.current_id is not None else [])

    def update_delete_controls(self, *_):
        ids = self.selected_record_ids()
        self.delete_button.setEnabled(bool(ids))
        self.delete_button.setText(f"删除 {len(ids)} 项" if len(ids) > 1 else "删除")
        self.delete_button.setToolTip("删除所选内容及其全部日期归档；Ctrl / Shift 可多选，列表中按 Delete 也可删除")

    def delete_selected(self):
        ids = self.selected_record_ids()
        if len(ids) == 1:
            self.delete_requested.emit(ids[0])
        elif ids:
            self.delete_many_requested.emit(ids)

    def open_record_menu(self, point):
        index = self.list_view.indexAt(point)
        row = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        if not row or row.get("header"):
            return
        if not self.list_view.selectionModel().isSelected(index):
            self.list_view.setCurrentIndex(index)
        else:
            # 右键已选中的另一行时保留多选，但复制/标记应针对实际点击的当前行。
            self.list_view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        menu = QMenu(self)
        menu.addAction("复制当前内容", self.copy_current)
        if row["kind"] == "text":
            menu.addAction("编辑内容", lambda: self.text_edit_requested.emit(row["id"]))
        else:
            menu.addAction("独立窗口查看图片", self.open_image_viewer)
            menu.addAction("编辑图片 / 更新原记录", lambda: self.image_edit_requested.emit("edit"))
        if self.current_id is not None:
            menu.addAction("取消固定" if row["pinned"] else "固定",
                           lambda: self.pin_requested.emit(self.current_id))
            menu.addAction("编辑特殊标记与标签" if row["marked"] else "添加特殊标记与标签",
                           lambda: self.mark_requested.emit(self.current_id))
        menu.addSeparator()
        menu.addAction(f"删除所选 {len(self.selected_record_ids())} 项  Delete", self.delete_selected)
        menu.exec(self.list_view.viewport().mapToGlobal(point))

    def update_shortcut_labels(self, *_):
        values = self.shortcut_store.bindings if self.shortcut_store else DEFAULT_SHORTCUTS
        self.screenshot_button.setText(f'截图  {values["screenshot"]}')
        self.shortcut_hint.setText(f'{values["history"]} 调出 · Esc 收起')
        if self.current_id is None and hasattr(self, "preview_stack"):
            self.select_record(QModelIndex())

    def move_selection(self, step):
        rows = [i for i, row in enumerate(self.model.rows) if not row.get("header")]
        if not rows:
            return
        current = self.list_view.currentIndex().row()
        place = rows.index(current) if current in rows else (-1 if step > 0 else len(rows))
        target = rows[min(max(place + step, 0), len(rows)-1)]
        self.list_view.setCurrentIndex(self.model.index(target, 0))
        self.list_view.scrollTo(self.model.index(target, 0))

    def confirm_clear(self):
        answer = QMessageBox.question(self, "清空普通记录",
            "清空所有日期中未固定、未特殊标记的文字和图片？\n"
            "此操作不受当前日期筛选限制，固定和特殊标记记录会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_requested.emit()

    def open_original(self):
        return self.open_image_viewer(original_size=True)

    def open_image_viewer(self, *, original_size=False):
        if self.current_image.isNull():
            return
        from image_viewer import ImageViewer
        viewer = ImageViewer(self.current_image, original_size=original_size)
        viewer.setWindowIcon(self.windowIcon())
        self.image_viewers.append(viewer)
        viewer.closed.connect(self.remove_image_viewer)
        viewer.show()
        return viewer

    def remove_image_viewer(self, viewer):
        if viewer in self.image_viewers:
            self.image_viewers.remove(viewer)

    def close_image_viewers(self):
        for viewer in tuple(self.image_viewers):
            viewer.close()

    def set_paused(self, paused):
        self.pause_action.setText("恢复记录" if paused else "暂停记录")
        self.status.setText("● 已暂停" if paused else "● 正在记录")
        self.status.setStyleSheet("color: #9a7639;" if paused else "color: #508a72;")

    def set_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def show_history(self):
        self.check_day()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        self.search.selectAll()

    def hide_window(self):
        self.hide()
        self.hidden.emit()

    def closeEvent(self, event):
        if self.exiting:
            self.close_image_viewers()
            event.accept()
        else:
            event.ignore()
            self.hide_window()
