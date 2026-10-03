import os
import zlib
import struct


def save_bgr_png(path: str, image) -> bool:
    """将 BGR 格式的截图保存为 PNG（纯标准库实现，不依赖 Pillow/CV2）

    Args:
        path: 保存路径，父目录不存在时自动创建
        image: numpy 数组，形状 (h, w, c>=3)，dtype uint8，通道顺序为 BGR

    Returns:
        bool: 保存成功返回 True；图像格式非法（空图/非 uint8/维度错误）返回 False
    """
    if image is None or getattr(image, "ndim", 0) != 3 or image.shape[2] < 3 or image.dtype != "uint8":
        return False

    height, width = image.shape[:2]
    if height == 0 or width == 0:  # 截图失败时 maa 返回 (0, 0, 3) 空数组
        return False

    # 每行前置过滤字节 0，同时把 BGR 翻转为 PNG 所需的 RGB（反转视图由 tobytes 按 C 序展开）
    raw = b"".join(b"\x00" + row.tobytes() for row in image[:, :, 2::-1])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    # IHDR: 8 位深，颜色类型 2（真彩 RGB），无隔行
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return True
