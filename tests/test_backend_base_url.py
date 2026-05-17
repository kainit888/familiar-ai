"""Tests for UTILITY_BASE_URL / SCENE_BASE_URL override (Phase C-1 引き継ぎ 1-1 パッチ)。

UTILITY_PLATFORM=openai のとき base_url を `.env` から指定できることを検証する。
未指定時は OpenAI 公式エンドポイント (https://api.openai.com/v1) にフォールバック。
"""

from __future__ import annotations

import os
from unittest.mock import patch

from familiar_agent.backend import (
    OpenAICompatibleBackend,
    create_scene_backend,
    create_utility_backend,
)
from familiar_agent.config import AgentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_with_utility(
    platform: str = "",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> AgentConfig:
    """UTILITY_* と utility_base_url を持つ最小限の AgentConfig を生成。"""
    config = AgentConfig.__new__(AgentConfig)
    config.api_key = "main-key"
    config.platform = "anthropic"
    config.model = "claude-haiku-4-5-20251001"
    config.utility_platform = platform
    config.utility_api_key = api_key
    config.utility_model = model
    config.utility_base_url = base_url
    config.scene_platform = ""
    config.scene_api_key = ""
    config.scene_model = ""
    config.scene_base_url = ""
    return config


def _config_with_scene(
    platform: str = "",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> AgentConfig:
    """SCENE_* と scene_base_url を持つ最小限の AgentConfig を生成。"""
    config = AgentConfig.__new__(AgentConfig)
    config.api_key = "main-key"
    config.platform = "anthropic"
    config.model = "claude-haiku-4-5-20251001"
    config.utility_platform = ""
    config.utility_api_key = ""
    config.utility_model = ""
    config.utility_base_url = ""
    config.scene_platform = platform
    config.scene_api_key = api_key
    config.scene_model = model
    config.scene_base_url = base_url
    return config


# ---------------------------------------------------------------------------
# Tests: AgentConfig defaults
# ---------------------------------------------------------------------------


def test_agent_config_utility_base_url_defaults_to_empty():
    """AgentConfig.utility_base_url は環境変数未設定なら空文字列。"""
    env = {k: v for k, v in os.environ.items() if k != "UTILITY_BASE_URL"}
    with patch.dict(os.environ, env, clear=True):
        config = AgentConfig()
    assert config.utility_base_url == ""


def test_agent_config_scene_base_url_from_env():
    """SCENE_BASE_URL が env から正しく読まれる。"""
    with patch.dict(
        os.environ,
        {
            "SCENE_PLATFORM": "openai",
            "SCENE_API_KEY": "ollama",
            "SCENE_BASE_URL": "http://192.168.10.104:11434/v1",
        },
    ):
        config = AgentConfig()
    assert config.scene_base_url == "http://192.168.10.104:11434/v1"


# ---------------------------------------------------------------------------
# Tests: create_utility_backend()
# ---------------------------------------------------------------------------


def test_create_utility_backend_openai_uses_custom_base_url():
    """UTILITY_PLATFORM=openai + UTILITY_BASE_URL 指定 → 指定 URL の OpenAICompat を返す。"""
    config = _config_with_utility(
        platform="openai",
        api_key="ollama",
        model="qwen2.5:1.5b",
        base_url="http://localhost:11434/v1",
    )
    result = create_utility_backend(config)
    assert isinstance(result, OpenAICompatibleBackend)
    # AsyncOpenAI.base_url は URL 型として保持されるため、文字列含有で確認。
    assert "11434" in str(result.client.base_url)


def test_create_utility_backend_openai_defaults_to_openai_api():
    """UTILITY_BASE_URL 未指定 → 公式 OpenAI エンドポイントにフォールバック。"""
    config = _config_with_utility(
        platform="openai",
        api_key="sk-test",
        model="gpt-4o-mini",
        base_url="",
    )
    result = create_utility_backend(config)
    assert isinstance(result, OpenAICompatibleBackend)
    assert "api.openai.com" in str(result.client.base_url)


# ---------------------------------------------------------------------------
# Tests: create_scene_backend()
# ---------------------------------------------------------------------------


def test_create_scene_backend_openai_uses_custom_base_url():
    """SCENE_PLATFORM=openai + SCENE_BASE_URL 指定 → 指定 URL の OpenAICompat を返す。"""
    config = _config_with_scene(
        platform="openai",
        api_key="ollama",
        model="qwen2.5:1.5b",
        base_url="http://192.168.10.104:11434/v1",
    )
    result = create_scene_backend(config)
    assert isinstance(result, OpenAICompatibleBackend)
    assert "192.168.10.104" in str(result.client.base_url)


def test_create_scene_backend_openai_defaults_to_openai_api():
    """SCENE_BASE_URL 未指定 → 公式 OpenAI エンドポイントにフォールバック。"""
    config = _config_with_scene(
        platform="openai",
        api_key="sk-test",
        model="gpt-4o-mini",
        base_url="",
    )
    result = create_scene_backend(config)
    assert isinstance(result, OpenAICompatibleBackend)
    assert "api.openai.com" in str(result.client.base_url)
