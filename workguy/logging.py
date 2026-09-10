"""极简统一日志。零第三方依赖，仅用标准库。

约定：
- ``get_logger(name)`` 返回配置好的标准库 ``logging.Logger``
- 默认输出到 stderr（StreamHandler）
- 日志级别从环境变量 ``WORKGUY_LOG_LEVEL`` 读取（默认 WARNING）
- 本模块只为库代码提供统一入口，**不修改任何其他模块**，也不在库里散落 print
"""

from __future__ import annotations

import logging
import os

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_CONFIGURED: set[str] = set()


def _level_from_env() -> int:
    raw = os.environ.get("WORKGUY_LOG_LEVEL", "WARNING").strip().upper()
    return _LEVELS.get(raw, logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """返回按环境配置好的 logger（每个 name 仅配置一次）。"""
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger

    level = _level_from_env()
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)

    logger.propagate = False
    _CONFIGURED.add(name)
    return logger
