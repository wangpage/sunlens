"""帧差去重（借 screenpipe 的 pHash 早退思路，用 Pillow 实现 aHash，无额外依赖）。

aHash：缩到 8x8 灰度，与均值比较得 64 bit 指纹；两帧汉明距离/64 即变化比例。
便宜、够用——只为决定"画面变没变、要不要落这一帧"，不追求语义精度。
"""

from __future__ import annotations

from PIL import Image

_HASH_SIZE = 8
_BITS = _HASH_SIZE * _HASH_SIZE


def average_hash(image: Image.Image, size: int = _HASH_SIZE) -> int:
    """返回 size*size 位的 aHash（整数）。"""
    img = image.convert("L").resize((size, size), Image.BILINEAR)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p >= avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    """两个 aHash 的汉明距离（不同位数）。"""
    return (a ^ b).bit_count()


def diff_ratio(a: int | None, b: int, bits: int = _BITS) -> float:
    """变化比例 0~1。a 为 None（第一帧）时返回 1.0。"""
    if a is None:
        return 1.0
    return hamming(a, b) / bits


def hash_to_hex(h: int) -> str:
    return format(h, "016x")
