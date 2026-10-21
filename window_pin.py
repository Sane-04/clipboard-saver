"""已有图钉按钮统一进入选窗模式，窗口上的标记负责取消置顶。"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QPushButton

from ui_theme import icon


class TopmostButton(QPushButton):
    requested = Signal()

    def __init__(self, window):
        super().__init__("置顶", window)
        self.setMinimumWidth(76)
        self.setToolTip("点击后选择要置顶的窗口；点击目标上的蓝色图钉取消置顶")
        self.setIcon(icon("pin"))
        self.clicked.connect(self.select_window)

    def select_window(self):
        self.requested.emit()
        controller = getattr(QApplication.instance(), "_window_pin_controller", None)
        if controller is not None:
            controller.start()
