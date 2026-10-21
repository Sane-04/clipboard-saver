"""纵向截图拼接：只接受可靠重叠，按新增小段保存，避免累计图反复复制。"""
from dataclasses import dataclass

import cv2
import numpy as np
from PySide6.QtGui import QImage, QPainter


def gray(image):
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    rows = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    rgb = rows[:, :image.width() * 3].reshape(image.height(), image.width(), 3)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def overlap_quality(previous, current, shift):
    old, new = previous[shift:], current[:len(current)-shift]
    difference = np.abs(old.astype(np.float32)-new.astype(np.float32))
    return float(np.mean(difference)), float(np.mean(difference <= 18))


def find_shift(previous, current):
    height, width = previous.shape
    gutter = min(20, width // 12)
    old, new = previous[:, gutter:width-gutter], current[:, gutter:width-gutter]
    whole_difference = np.abs(old.astype(np.float32)-new.astype(np.float32))
    if np.mean(whole_difference) < .6 and np.mean(whole_difference <= 12) > .995:
        return "duplicate", 0
    # 短视口不能套用固定40像素重叠；保留多行像素校验，仍拒绝重复纹理的歧义。
    band = max(5, min(80, height // 10))
    minimum_overlap = max(band + 4, min(40, max(12, height // 6)))
    candidates = set()
    for y in (0, height // 5, height // 3, height // 2):
        template = new[y:y+band]
        if template.std() < 3:
            continue
        scores = cv2.matchTemplate(old, template, cv2.TM_CCOEFF_NORMED)[:, 0]
        # 保留彼此分离的峰值，重复文字/图案有多个解时不能擅自选择最高峰。
        for _ in range(5):
            position = int(np.argmax(scores))
            if scores[position] < .90:
                break
            shift = position-y
            if 1 <= shift <= height-minimum_overlap:
                candidates.add(shift)
            scores[max(0, position-3):position+4] = -1
    valid = []
    for shift in candidates:
        error, agreement = overlap_quality(old, new, shift)
        if error <= 3 and agreement >= .98 and new[:height-shift].std() >= 3:
            valid.append((error, shift))
    valid.sort()
    if valid:
        if len(valid) > 1 and abs(valid[0][0]-valid[1][0]) < .5:
            return "ambiguous", 0
        return "append", valid[0][1]
    return "unmatched", 0


@dataclass(frozen=True)
class StitchResult:
    status: str
    height: int
    added: int = 0


class VerticalStitcher:
    def __init__(self, first, *, max_height=30000, max_pixels=30000000):
        if first.isNull() or first.width() < 50 or first.height() < 40:
            raise ValueError("选区太小，请框选至少 50×40 像素的滚动内容。")
        if first.height() > max_height or first.width()*first.height() > max_pixels:
            raise ValueError("选区超过容量上限，请缩小截图范围。")
        self.parts = [first.copy()]
        # 捕获图带屏幕 DPR；拼接画布的单位是原始像素，不能让 QPainter 再缩放一次。
        self.parts[0].setDevicePixelRatio(1)
        self.previous = first.copy()
        self.previous.setDevicePixelRatio(1)
        self.width, self.height = first.width(), first.height()
        self.max_height, self.max_pixels = max_height, max_pixels

    def add(self, image):
        if image.size() != self.previous.size():
            return StitchResult("size_changed", self.height)
        status, shift = find_shift(gray(self.previous), gray(image))
        if status != "append":
            return StitchResult(status, self.height)
        height = self.height+shift
        if height > self.max_height or self.width*height > self.max_pixels:
            return StitchResult("limit", self.height)
        part = image.copy(0, image.height()-shift, self.width, shift)
        part.setDevicePixelRatio(1)
        self.parts.append(part)
        self.previous = image.copy()
        self.previous.setDevicePixelRatio(1)
        self.height = height
        return StitchResult("append", self.height, shift)

    def render(self, tail=None):
        size = self.height if tail is None else min(self.height, tail)
        result = QImage(self.width, size, QImage.Format.Format_ARGB32)
        result.fill(0)
        result.setColorSpace(self.parts[0].colorSpace())
        painter = QPainter(result)
        y = -(self.height-size)
        for part in self.parts:
            if y+part.height() > 0:
                painter.drawImage(0, y, part)
            y += part.height()
        painter.end()
        return result

    def undo(self):
        if len(self.parts) < 2:
            return False
        self.height -= self.parts.pop().height()
        # 只重建一个视口大小的基准，不保留每一帧全图造成内存倍增。
        self.previous = self.render(self.previous.height())
        return True
