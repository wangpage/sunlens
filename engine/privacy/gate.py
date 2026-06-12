"""出口闸门：任何「发图上云」之前必须经过这里。

借鉴 openadapt-desktop/engine/review.py 的单一出口检查点思想。M0 阶段实现最小版：
- 一帧必须先被标记为「已脱敏」(scrubbed) 才允许外发；
- redact 关闭时要求显式 allow_unredacted=True，避免误把原图发出去。

M2+ 会接上完整的录制状态机（CAPTURED→SCRUBBED→REVIEWED）。
"""

from __future__ import annotations


class EgressBlockedError(RuntimeError):
    """未通过脱敏闸门就试图外发，拦截。"""


def check_egress_allowed(*, scrubbed: bool, redact_enabled: bool, allow_unredacted: bool = False) -> None:
    """外发前校验。不满足直接抛错，阻止上云。

    参数：
        scrubbed: 这帧是否已跑过脱敏流程。
        redact_enabled: 配置是否开启了涂码。
        allow_unredacted: 当涂码关闭时，必须显式置 True 才放行（M0 peek 调试用）。
    """
    if not scrubbed:
        raise EgressBlockedError("该帧尚未经过脱敏，禁止外发到云端。")
    if not redact_enabled and not allow_unredacted:
        raise EgressBlockedError(
            "涂码已关闭(redact_enabled=False)，禁止外发未涂码原图；"
            "如确需调试，请显式传 allow_unredacted=True。"
        )
