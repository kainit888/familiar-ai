"""Phase C-6: setup_logging() が loguru → app.log の二層統合を成立させること。

pico_agent.* は loguru、familiar_agent.* は stdlib logging を使う。
両者が同一の ~/.cache/familiar-ai/app.log に書き出されることを確認する。
InterceptHandler は **意図的に追加しない** (二重ログ回避)。
"""

from __future__ import annotations

import logging
from pathlib import Path

from loguru import logger as loguru_logger


def test_loguru_logs_reach_app_log(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # 既存 root handler を一旦リセット (test 独立性確保)
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)

    from familiar_agent import main as mainmod

    mainmod.setup_logging(debug=False)
    loguru_logger.info("phase_c6_loguru_marker")
    loguru_logger.complete()  # flush enqueue queue
    logging.getLogger("test_c6").info("phase_c6_stdlog_marker")
    # FileHandler のバッファを flush
    for h in logging.getLogger().handlers:
        h.flush()

    log_path = tmp_path / ".cache" / "familiar-ai" / "app.log"
    log = log_path.read_text(encoding="utf-8")
    assert "phase_c6_loguru_marker" in log, f"loguru log not in {log_path}: {log[:300]}"
    assert "phase_c6_stdlog_marker" in log, f"std log not in {log_path}: {log[:300]}"
