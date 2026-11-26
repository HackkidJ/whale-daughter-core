"""日志配置：容器里输出 JSON，方便 Loki/检索；本地 CLI 用控制台格式。"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str, *, json_output: bool = True) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric, format="%(message)s", stream=sys.stderr)
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_cli_logging(verbose: bool = False) -> None:
    """CLI 默认只报错，免得把聊天界面刷花。"""
    configure_logging("INFO" if verbose else "WARNING", json_output=False)
