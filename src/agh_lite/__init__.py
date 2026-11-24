"""AGH Lite：个人版 Token 经济工具箱。

从 AGH 拆出来的单用户版本：只保留「预算、缓存、去重、门控、账本」，
去掉多智能体竞技场、签名网关、冻结层与八状态治理机。

四件事：
1. ``audit``   只读扫描 DSH 会话，找出 token 浪费点；
2. ``pack``    在预算内打包上下文，自动去重；
3. ``cache``   内容寻址的响应缓存，命中即不重复付费；
4. ``budget``  调用前预授权、调用后结算的硬闸门。

设计取舍见 ``docs/DESIGN.md``。
"""

from __future__ import annotations

__version__ = "0.12.0"
__all__ = ["__version__"]
