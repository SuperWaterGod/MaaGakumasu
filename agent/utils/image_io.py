"""通过 MaaFramework 读写 BGR 图像，无需额外图像库。

SDK 尚未封装编码缓冲区接口；私有句柄访问和 C 接口签名统一放在此处，
并在所属 ImageBuffer 释放前复制编码字节。
"""

import ctypes
import threading
from pathlib import Path

import numpy as np
from maa.buffer import ImageBuffer
from maa.define import MaaBool, MaaSize, MaaImageBufferHandle
from maa.library import Library

_API_LOCK = threading.Lock()
_bound_library = None


def _codec_api():
    global _bound_library
    library = Library.framework()
    with _API_LOCK:
        if _bound_library is not library:
            library.MaaImageBufferGetEncoded.restype = ctypes.c_void_p
            library.MaaImageBufferGetEncoded.argtypes = [MaaImageBufferHandle]
            library.MaaImageBufferGetEncodedSize.restype = MaaSize
            library.MaaImageBufferGetEncodedSize.argtypes = [MaaImageBufferHandle]
            library.MaaImageBufferSetEncoded.restype = MaaBool
            library.MaaImageBufferSetEncoded.argtypes = [MaaImageBufferHandle, ctypes.c_void_p, MaaSize]
            _bound_library = library
    return library


def bgr_image(image):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or not image.size:
        raise ValueError("Expected a nonempty uint8 BGR image")
    return np.ascontiguousarray(image)


def encode_png(image):
    library = _codec_api()
    buffer = ImageBuffer()
    if not buffer.set(bgr_image(image)):
        raise RuntimeError("Cannot set Maa image buffer")
    pointer = library.MaaImageBufferGetEncoded(buffer._handle)
    size = library.MaaImageBufferGetEncodedSize(buffer._handle)
    if not pointer or not size:
        raise ValueError("Cannot encode Maa image buffer")
    encoded = ctypes.string_at(pointer, size)
    if not encoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Maa image encoder did not return PNG")
    return encoded


def decode_image(encoded):
    if not encoded:
        raise ValueError("Empty encoded image")
    library = _codec_api()
    buffer = ImageBuffer()
    source = ctypes.create_string_buffer(encoded)
    if not library.MaaImageBufferSetEncoded(buffer._handle, source, len(encoded)):
        raise ValueError("Cannot decode Maa image buffer")
    return bgr_image(buffer.get())


def read_image(path):
    return decode_image(Path(path).read_bytes())


def resize_image(image, width, height):
    image = bgr_image(image)
    if width <= 0 or height <= 0:
        raise ValueError("Invalid image size")
    if image.shape[:2] == (height, width):
        return image
    buffer = ImageBuffer()
    if not buffer.set(image) or not buffer.resize(width, height):
        raise ValueError("Cannot resize Maa image buffer")
    return bgr_image(buffer.get())
