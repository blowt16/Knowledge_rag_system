"""图片过滤器 — 识别并跳过无意义的装饰图/图标/条码/二维码。

配置驱动，从 chroma.yaml 读取阈值，支持：
  - 尺寸过滤：宽/高/面积/宽高比
  - 颜色特征过滤：颜色方差、边缘密度
  - 条码检测：pyzbar 精确识别 QR/DataMatrix/Code128/EAN 等
  - 位置过滤：页眉页脚/页边距（预留接口）
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from PIL import Image as PILImage

from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

# ============================================================
# 特征提取
# ============================================================


def _compute_color_variance(img: PILImage.Image) -> float:
    """计算图像颜色方差 — 装饰元素通常颜色单一，方差极低。

    Returns:
        0~1 之间的归一化方差值（RGB 三通道平均）。
    """
    try:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        pixels = list(img.getdata())
        n = len(pixels)

        # 分别计算 RGB 各通道方差
        r_sum = sum(p[0] for p in pixels)
        g_sum = sum(p[1] for p in pixels)
        b_sum = sum(p[2] for p in pixels)
        r_mean = r_sum / n
        g_mean = g_sum / n
        b_mean = b_sum / n

        r_var = sum((p[0] - r_mean) ** 2 for p in pixels) / n
        g_var = sum((p[1] - g_mean) ** 2 for p in pixels) / n
        b_var = sum((p[2] - b_mean) ** 2 for p in pixels) / n

        # 归一化到 0~1（最大可能方差 = 255²/4 ≈ 16256）
        max_var = 16256.25
        return (r_var + g_var + b_var) / (3 * max_var)
    except Exception:
        return 1.0  # 计算失败默认保留


def _compute_edge_density(img: PILImage.Image) -> float:
    """计算边缘密度 — 条码/二维码边缘密集，纯色装饰边缘稀疏。

    使用 Sobel 算子的简化版本（3x3 卷积），不依赖 OpenCV。

    Returns:
        0~1 之间的边缘像素占比。
    """
    try:
        if img.mode != "L":
            img = img.convert("L")
        w, h = img.size
        if w < 3 or h < 3:
            return 0.0

        pixels = list(img.getdata())
        edge_count = 0
        total = (w - 2) * (h - 2)
        if total <= 0:
            return 0.0

        for y in range(1, h - 1):
            for x in range(1, w - 1):
                # 简化 Sobel: 水平 + 垂直梯度
                idx = y * w + x
                gx = abs(pixels[idx + 1] - pixels[idx - 1])
                gy = abs(pixels[idx + w] - pixels[idx - w])
                if gx + gy > 50:  # 梯度阈值
                    edge_count += 1

        return edge_count / total
    except Exception:
        return 1.0  # 计算失败默认保留


# ============================================================
# 图片过滤器
# ============================================================


class ImageFilter:
    """配置驱动的图片过滤器 — 识别无意义装饰/图标/码图。

    Usage:
        f = ImageFilter()
        skip, reason = f.should_skip(img_data, "img_0.png", is_referenced=False)
    """

    def __init__(self, **overrides: Any):
        """从 chroma.yaml 加载配置，允许通过 kwargs 覆盖。"""
        self._enabled = overrides.get("enabled", _cfg("image_filter_enabled", True))
        self._min_w = overrides.get("min_width", _cfg("image_filter_min_width", 30))
        self._min_h = overrides.get("min_height", _cfg("image_filter_min_height", 30))
        self._max_ratio = overrides.get("max_aspect_ratio", _cfg("image_filter_max_aspect_ratio", 20))
        self._min_area = overrides.get("min_area", _cfg("image_filter_min_area", 900))
        self._min_color_var = overrides.get(
            "min_color_variance", _cfg("image_filter_min_color_variance", 0.005)
        )
        self._max_edge_density = overrides.get(
            "max_edge_density", _cfg("image_filter_max_edge_density", 0.95)
        )
        self._barcode_enabled = overrides.get(
            "enable_barcode_detection", _cfg("image_filter_enable_barcode_detection", True)
        )
        self._barcode_types = overrides.get(
            "barcode_types", _cfg("image_filter_barcode_types", ["QRCODE"])
        )

        # 统计计数器
        self._stats: dict[str, int] = {
            "total": 0, "saved": 0, "skipped_size": 0,
            "skipped_barcode": 0, "skipped_color": 0,
        }

        if not self._enabled:
            logger.info("【图片过滤】已禁用 (image_filter_enabled=false)")

    def should_skip(
        self, img_data: bytes, img_name: str, is_referenced: bool
    ) -> tuple[bool, str]:
        """判断图片是否应跳过。

        Args:
            img_data: 图片原始字节
            img_name: MinerU 内部文件名 (如 images/img_0.png)
            is_referenced: content_list 中是否有 image/table 块引用此图

        Returns:
            (skip: bool, reason: str)
        """
        self._stats["total"] += 1

        if not self._enabled:
            self._stats["saved"] += 1
            return False, ""

        try:
            img = PILImage.open(BytesIO(img_data))
            w, h = img.size
            area = w * h
            size_kb = len(img_data) / 1024
            ratio = w / max(h, 1)

            # ── ① 尺寸过滤（全局：极小尺寸跳过) ──
            skip, reason = self._check_size(w, h, area, ratio, size_kb, is_referenced)
            if skip:
                self._stats["skipped_size"] += 1
                return True, reason

            # ── ② 条码检测（全局：条码/二维码一律跳过，含已引用） ──
            skip, reason = self._check_barcode(img, img_name, w, h)
            if skip:
                self._stats["skipped_barcode"] += 1
                return True, reason

            # 已引用图片不再检查以下条件
            if is_referenced:
                self._stats["saved"] += 1
                return False, ""

            # ── ③ 颜色特征过滤（仅未引用） ──
            skip, reason = self._check_color(img, w, h)
            if skip:
                self._stats["skipped_color"] += 1
                return True, reason

        except Exception:
            pass

        self._stats["saved"] += 1
        return False, ""

    # ---- 统计 ----

    def get_stats(self) -> dict[str, int]:
        """返回当前累计统计。"""
        return dict(self._stats)

    def reset_stats(self) -> None:
        """重置统计计数器（新文件上传前调用）。"""
        self._stats = {k: 0 for k in self._stats}

    def log_summary(self) -> None:
        """输出图片过滤汇总日志。"""
        s = self._stats
        skipped = s["skipped_size"] + s["skipped_barcode"] + s["skipped_color"]
        if s["total"] == 0:
            return
        parts = [
            f"总数={s['total']}",
            f"保存={s['saved']}",
            f"跳过={skipped}",
        ]
        details = []
        if s["skipped_barcode"]:
            details.append(f"条码/二维码={s['skipped_barcode']}")
        if s["skipped_size"]:
            details.append(f"尺寸/比例={s['skipped_size']}")
        if s["skipped_color"]:
            details.append(f"颜色单一={s['skipped_color']}")
        if details:
            parts.append("(" + ", ".join(details) + ")")
        logger.info("【图片过滤】汇总: " + " ".join(parts))

    # ---- 子检测 ----

    def _check_size(
        self, w: int, h: int, area: int, ratio: float, size_kb: float, is_referenced: bool,
    ) -> tuple[bool, str]:
        """尺寸/比例过滤。"""
        # 极小尺寸 — 追踪像素（无论是否引用都跳过）
        if w < self._min_w and h < self._min_h:
            return True, f"极小尺寸 ({w}x{h})"

        if not is_referenced:
            # 极小面积
            if area < self._min_area:
                return True, f"面积过小 ({area}px² < {self._min_area})"

            # 极端宽高比 — 装饰线
            if ratio > self._max_ratio or ratio < (1.0 / self._max_ratio):
                return True, f"极端宽高比 ({w}x{h} ratio={ratio:.1f})"

        return False, ""

    def _check_color(self, img: PILImage.Image, w: int, h: int) -> tuple[bool, str]:
        """颜色特征过滤 — 纯色/低方差装饰图。"""
        if self._min_color_var <= 0:
            return False, ""

        if w * h > 40000:  # 仅对小图检测颜色方差，大图跳过（性能考虑）
            return False, ""

        variance = _compute_color_variance(img)
        if variance < self._min_color_var:
            return True, f"颜色单一 (方差={variance:.4f} < {self._min_color_var})"

        return False, ""

    def _check_barcode(
        self, img: PILImage.Image, img_name: str, w: int, h: int,
    ) -> tuple[bool, str]:
        """条码/二维码检测。"""
        if not self._barcode_enabled:
            return False, ""

        try:
            from pyzbar.pyzbar import decode as pyzbar_decode

            codes = pyzbar_decode(img)
            if not codes:
                return False, ""

            # 按配置的码型过滤
            if self._barcode_types:
                codes = [c for c in codes if c.type in self._barcode_types]

            if codes:
                types = ", ".join(sorted(set(c.type for c in codes)))
                return True, f"条码/二维码 ({types} {w}x{h})"
        except ImportError:
            logger.debug("【图片过滤】pyzbar 未安装，跳过条码检测")
        except Exception as e:
            logger.debug(f"【图片过滤】条码检测异常: {e}")

        return False, ""


# ============================================================
# 模块级单例 + 快捷函数
# ============================================================

_filter_instance: ImageFilter | None = None


def get_image_filter(**overrides: Any) -> ImageFilter:
    """获取全局图片过滤器单例。"""
    global _filter_instance
    if _filter_instance is None or overrides:
        _filter_instance = ImageFilter(**overrides)
    return _filter_instance


def should_skip_image(
    img_data: bytes, img_name: str, is_referenced: bool,
) -> tuple[bool, str]:
    """快捷函数 — 判断图片是否应跳过。"""
    return get_image_filter().should_skip(img_data, img_name, is_referenced)


def _cfg(key: str, default: Any) -> Any:
    """读取配置，异常时返回默认值。"""
    try:
        return get_config(key, default)
    except Exception:
        return default
