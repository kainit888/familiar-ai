"""Phase H: web_knowledge → self_narrative 統合の重複防止 ledger。

Phase F で observations.db に保存された web_knowledge (kind="web_knowledge") の
うち、どれを既に self_narrative (一人称日記) に統合したかを記録する軽量 sidecar。
``recall_web_knowledge`` は行 id を返さないため、安定キーである
``WebKnowledge.query`` 文字列で「統合済み」を管理する (Phase F の保存層は無改変)。

self_narrative.py / emotion 系と同じ ``~/.familiar_ai/`` 配下 json 永続パターン。
読み書き失敗は raise せず ``logger.warning`` + 続行 (graceful)。0 件運用なら disk に
一切触れない。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".familiar_ai" / "web_knowledge_integrated.json"


class WebKnowledgeLedger:
    """統合済み web_knowledge を query 文字列で記録する sidecar ledger。"""

    def __init__(self, path: Path | None = None):
        self._path = path or _DEFAULT_PATH

    def _load(self) -> set[str]:
        if not self._path.exists():
            return set()
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
            queries = data.get("queries", []) if isinstance(data, dict) else []
            return {str(q) for q in queries}
        except Exception as e:
            logger.warning("Could not read web_knowledge ledger: %s", e)
            return set()

    def seen(self, query: str) -> bool:
        """この query を既に self_narrative へ統合済みか。"""
        return query in self._load()

    def mark(self, query: str) -> None:
        """この query を統合済みとして記録 (失敗しても raise しない)。"""
        try:
            queries = self._load()
            queries.add(query)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as f:
                json.dump({"queries": sorted(queries)}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("Could not write web_knowledge ledger: %s", e)
