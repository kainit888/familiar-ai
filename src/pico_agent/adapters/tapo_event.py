"""Tapo C210 ONVIF イベント受信層 — Phase X Stage C。

カメラ自身の motion / person 検知 (ONVIF event) を購読し、典型イベントを
``on_event`` callback へ流す常駐タスクを起動する。STT の常時購読
(``stt_kotoba.start_rtsp_subscription``) と同型の設計。

設計思想 (Phase X):
    Tapo = bottom-up 知覚 (「何か動いた」)、desire = 注意、LLM = 判断。
    本層は **イベントを emit するだけ**。「何が変わったか」「喋るべきか」は
    familiar_agent 側が desire を boost し、ピコが自分のターンで see/判断する
    (= 軽量方式)。本層は同期 see()/vision を一切呼ばない。

二層分離 (絶対遵守、stt_kotoba / response_filter と同型):
    - ``familiar_agent`` を一切 import しない
    - 依存は stdlib + ``onvif`` (onvif-zeep-async) + ``aiohttp`` + ``loguru`` のみ
    - 例外は raise せず silent-fail + ``logger.warning``
    - 依存欠落 / mode=disabled なら即終了する no-op タスクを返す

公開 I/F:
    class TapoEvent  … 受信イベント (event_type / timestamp / topic / raw)
    async def start_event_subscription(on_event, *, host=None, ...) -> asyncio.Task

モード (``TAPO_EVENT_MODE``):
    - ``pullpoint`` (既定) … Pi が CreatePullPointSubscription → PullMessages で pull。
      inbound サーバ不要。TTL 更新は onvif manager が自動。
    - ``webhook`` … Pi 側 aiohttp サーバを立て、カメラに push させる
      (PullPoint が firmware で動かない C210 向けフォールバック)。
      ``TAPO_EVENT_WEBHOOK_HOST`` (カメラから到達可能な Pi の IP) 必須。
    - ``disabled`` … no-op。

実機検証 (Stage D) が必要な点 (unit test は mock で回避):
    - C210 が PullPoint を実際に確立し message を配信するか (firmware 依存)
    - 実際の ONVIF Topic 文字列 / person 検知対応有無 (``TapoEvent.topic`` に保持)
    - 誤発火頻度 → DEBOUNCE / PERSON_ONLY / boost 量で調整
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from loguru import logger

try:  # onvif-zeep-async は camera group の任意依存。欠落しても本体は起動する。
    from onvif import ONVIFCamera

    _ONVIF_AVAILABLE = True
except Exception:  # pragma: no cover - import 失敗環境用
    ONVIFCamera = None  # type: ignore[assignment,misc]
    _ONVIF_AVAILABLE = False


# ── 既定値 (環境変数で上書き可) ──────────────────────────────────────────────
_DEFAULT_MODE = "pullpoint"
_DEFAULT_DEBOUNCE_SEC = 30.0
_DEFAULT_PERSON_ONLY = False
_DEFAULT_RECONNECT_BACKOFF_SEC = 5.0
_DEFAULT_RENEW_SEC = 60.0
_DEFAULT_PULL_TIMEOUT_SEC = 60.0
_DEFAULT_ONVIF_PORT = 2020
_DEFAULT_WEBHOOK_PORT = 50080
_WEBHOOK_PATH = "/onvif_event"

# Topic / SimpleItem 分類ヒント (実機 Topic は Stage D で確認、topic は raw 保持)。
_PERSON_HINTS = ("peopledetector", "humandetector", "person", "human")
_PERSON_ITEM_NAMES = ("ispeople", "ishuman", "isperson")
_PERSON_CLASS_VALUES = ("human", "person")
_MOTION_HINTS = ("motion", "cellmotiondetector", "motionalarm")
_MOTION_ITEM_NAMES = ("ismotion", "state", "motionalarm")
_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")


@dataclass(frozen=True)
class TapoEvent:
    """カメラ由来の 1 イベント (二層境界を渡る型)。"""

    event_type: str  # "motion" | "person"
    timestamp: float  # Pi 受信時刻 (time.time())。カメラ時計は使わない (NTP ズレ回避)。
    topic: str = ""  # 生 ONVIF Topic 文字列 (Stage D 分類監査用)
    raw: dict[str, str] | None = None  # parse 済 SimpleItem (診断用)


# ── 環境変数アクセサ (stt_kotoba 慣習: 未設定/不正値は既定へフォールバック) ────
def _env_any(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def _get_event_mode() -> str:
    return os.environ.get("TAPO_EVENT_MODE", _DEFAULT_MODE).strip().lower() or _DEFAULT_MODE


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _get_debounce_sec() -> float:
    return _get_float("TAPO_EVENT_DEBOUNCE_SEC", _DEFAULT_DEBOUNCE_SEC)


def _get_reconnect_backoff_sec() -> float:
    return _get_float("TAPO_EVENT_RECONNECT_BACKOFF_SEC", _DEFAULT_RECONNECT_BACKOFF_SEC)


def _get_renew_sec() -> float:
    return _get_float("TAPO_EVENT_RENEW_SEC", _DEFAULT_RENEW_SEC)


def _get_pull_timeout_sec() -> float:
    return _get_float("TAPO_EVENT_PULL_TIMEOUT_SEC", _DEFAULT_PULL_TIMEOUT_SEC)


def _get_person_only() -> bool:
    raw = os.environ.get("TAPO_EVENT_PERSON_ONLY", "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return _DEFAULT_PERSON_ONLY


def _get_webhook_host() -> str:
    # カメラから到達可能な Pi の IP。自動検出は不確実なので必須・env 指定。
    return _env_any("TAPO_EVENT_WEBHOOK_HOST")


def _get_webhook_port() -> int:
    raw = os.environ.get("TAPO_EVENT_WEBHOOK_PORT", "")
    try:
        return int(raw) if raw else _DEFAULT_WEBHOOK_PORT
    except ValueError:
        return _DEFAULT_WEBHOOK_PORT


def _get_onvif_conn() -> tuple[str, str, str, int]:
    """ONVIF 接続情報を CAMERA_* / TAPO_* env から取得。host が RTSP URL でも host 抽出。"""
    host = _env_any("CAMERA_HOST", "TAPO_CAMERA_HOST")
    user = _env_any("CAMERA_USERNAME", "TAPO_USERNAME")
    password = _env_any("CAMERA_PASSWORD", "TAPO_PASSWORD")
    port_raw = _env_any("CAMERA_ONVIF_PORT", "TAPO_ONVIF_PORT", default=str(_DEFAULT_ONVIF_PORT))
    try:
        port = int(port_raw)
    except ValueError:
        port = _DEFAULT_ONVIF_PORT
    if "://" in host:
        host = urlparse(host).hostname or host
    return host, user, password, port


# ── ONVIF WSDL ディレクトリ (camera.py の workaround と同一) ──────────────────
def _wsdl_dir() -> str:
    import onvif as _onvif_mod

    onvif_dir = os.path.dirname(_onvif_mod.__file__)
    wsdl_dir = os.path.join(onvif_dir, "wsdl")
    if not os.path.isdir(wsdl_dir):
        wsdl_dir = os.path.join(os.path.dirname(onvif_dir), "wsdl")
    return wsdl_dir


# ── 分類 / 抽出 (純関数、最もテストしやすい部分) ──────────────────────────────
def _classify(topic: str, simple_items: dict[str, str]) -> str | None:
    """ONVIF Topic + SimpleItem から "motion" / "person" を判定。不明/立下りは None。

    person を motion より優先。motion state が明示的に偽 (IsMotion=false 等) の
    「立下り (motion ended)」は None (立上りのみ採用)。
    """
    topic_l = (topic or "").lower()
    items_l = {(k or "").lower(): (v or "").lower() for k, v in simple_items.items()}

    # person 優先
    if any(h in topic_l for h in _PERSON_HINTS):
        return "person"
    for name in _PERSON_ITEM_NAMES:
        if items_l.get(name) in _TRUTHY:
            return "person"
    if items_l.get("objecttype") in _PERSON_CLASS_VALUES or items_l.get("class") in _PERSON_CLASS_VALUES:
        return "person"

    # motion の立下りは無視 (rising edge のみ)
    for name in _MOTION_ITEM_NAMES:
        if name in items_l and items_l[name] in _FALSY:
            return None
    if any(h in topic_l for h in _MOTION_HINTS):
        return "motion"
    for name in _MOTION_ITEM_NAMES:
        if items_l.get(name) in _TRUTHY:
            return "motion"
    return None


def _extract_topic(notification_message: Any) -> str:
    """zeep NotificationMessage から Topic 文字列を防御的に取り出す。"""
    try:
        topic = notification_message.Topic
        val = getattr(topic, "_value_1", None)
        if val is None:
            val = topic
        return str(val or "").strip()
    except Exception:
        return ""


def _extract_simple_items(notification_message: Any) -> dict[str, str]:
    """zeep NotificationMessage の Message/Data/SimpleItem を {Name: Value} に。"""
    items: dict[str, str] = {}
    try:
        data = notification_message.Message.Message.Data
        simple = getattr(data, "SimpleItem", None) or []
        for si in simple:
            name = getattr(si, "Name", None)
            val = getattr(si, "Value", None)
            if name is not None:
                items[str(name)] = "" if val is None else str(val)
    except Exception:
        pass
    return items


def _parse_soap_notifications(body: bytes) -> list[tuple[str, dict[str, str]]]:
    """webhook POST の SOAP 本文から (topic, {Name: Value}) を namespace 非依存で抽出。"""
    out: list[tuple[str, dict[str, str]]] = []

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(body)
    except Exception:
        return out
    for nm in root.iter():
        if _local(nm.tag) != "NotificationMessage":
            continue
        topic = ""
        items: dict[str, str] = {}
        for el in nm.iter():
            lt = _local(el.tag)
            if lt == "Topic":
                topic = (el.text or "").strip()
            elif lt == "SimpleItem":
                name = el.get("Name")
                if name is not None:
                    items[name] = el.get("Value") or ""
        out.append((topic, items))
    return out


# ── デバウンス + emit ────────────────────────────────────────────────────────
class _Debouncer:
    """type 毎に直近 emit からの窓内連続発火を 1 回へ畳む。"""

    def __init__(self, window_sec: float) -> None:
        self._window = window_sec
        self._last: dict[str, float] = {}

    def allow(self, event_type: str, now: float) -> bool:
        last = self._last.get(event_type, 0.0)
        if now - last < self._window:
            return False
        self._last[event_type] = now
        return True


async def _emit(
    on_event: Callable[[TapoEvent], Awaitable[None]],
    event_type: str,
    topic: str,
    raw: dict[str, str],
    *,
    debouncer: _Debouncer,
    person_only: bool,
    now: float | None = None,
) -> None:
    if person_only and event_type != "person":
        return
    t = now if now is not None else time.time()
    if not debouncer.allow(event_type, t):
        logger.debug("tapo_event: debounced {} (topic={!r})", event_type, topic)
        return
    evt = TapoEvent(event_type=event_type, timestamp=t, topic=topic, raw=raw or None)
    try:
        await on_event(evt)
    except Exception as e:  # callback の失敗でループを殺さない
        logger.warning("tapo_event: on_event callback failed: {}", e)


# ── ONVIF 接続 ───────────────────────────────────────────────────────────────
async def _connect_onvif() -> Any | None:
    host, user, password, port = _get_onvif_conn()
    if not host or not user or not password:
        logger.warning("tapo_event: CAMERA_HOST/USERNAME/PASSWORD incomplete")
        return None
    try:
        cam = ONVIFCamera(host, port, user, password, wsdl_dir=_wsdl_dir())
        await cam.update_xaddrs()
        return cam
    except Exception as e:
        logger.warning("tapo_event: ONVIF connect failed ({}:{}): {}", host, port, e)
        return None


# ── PullPoint 経路 ───────────────────────────────────────────────────────────
async def _run_one_pullpoint_session(
    on_event: Callable[[TapoEvent], Awaitable[None]],
    *,
    debounce_sec: float,
    person_only: bool,
    renew_sec: float,
    pull_timeout_sec: float,
) -> None:
    """1 PullPoint subscription を確立し PullMessages を回す。例外/購読断で戻る。"""
    cam = await _connect_onvif()
    if cam is None:
        return
    lost = asyncio.Event()
    try:
        manager = await cam.create_pullpoint_manager(
            _dt.timedelta(seconds=renew_sec), lambda: lost.set()
        )
    except Exception as e:
        logger.warning("tapo_event: create_pullpoint_manager failed: {}", e)
        return
    service = manager.get_service()
    debouncer = _Debouncer(debounce_sec)
    logger.info("tapo_event: PullPoint subscription established")
    try:
        while not lost.is_set():
            try:
                resp = await service.PullMessages(
                    {
                        "Timeout": _dt.timedelta(seconds=pull_timeout_sec),
                        "MessageLimit": 100,
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("tapo_event: PullMessages failed: {}", e)
                break
            for nm in getattr(resp, "NotificationMessage", None) or []:
                topic = _extract_topic(nm)
                items = _extract_simple_items(nm)
                etype = _classify(topic, items)
                if etype:
                    await _emit(
                        on_event,
                        etype,
                        topic,
                        items,
                        debouncer=debouncer,
                        person_only=person_only,
                    )
    finally:
        try:
            await manager.shutdown()
        except Exception:
            pass


# ── webhook 経路 ─────────────────────────────────────────────────────────────
async def _run_one_webhook_session(
    on_event: Callable[[TapoEvent], Awaitable[None]],
    *,
    debounce_sec: float,
    person_only: bool,
    renew_sec: float,
    webhook_host: str,
    webhook_port: int,
) -> None:
    """Pi 側 aiohttp サーバを立て、カメラに push 購読させる。購読断/cancel で戻る。"""
    cam = await _connect_onvif()
    if cam is None:
        return
    debouncer = _Debouncer(debounce_sec)
    lost = asyncio.Event()

    async def _handle(request: web.Request) -> web.Response:
        try:
            body = await request.read()
            for topic, items in _parse_soap_notifications(body):
                etype = _classify(topic, items)
                if etype:
                    await _emit(
                        on_event,
                        etype,
                        topic,
                        items,
                        debouncer=debouncer,
                        person_only=person_only,
                    )
        except Exception as e:  # 受信処理の失敗でサーバを落とさない
            logger.warning("tapo_event: webhook handler failed: {}", e)
        return web.Response(text="")

    app = web.Application()
    app.router.add_post(_WEBHOOK_PATH, _handle)
    runner = web.AppRunner(app)
    manager = None
    try:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", webhook_port)
        await site.start()
        address = f"http://{webhook_host}:{webhook_port}{_WEBHOOK_PATH}"
        try:
            manager = await cam.create_notification_manager(
                address, _dt.timedelta(seconds=renew_sec), lambda: lost.set()
            )
        except Exception as e:
            logger.warning("tapo_event: create_notification_manager failed: {}", e)
            return
        logger.info("tapo_event: webhook subscription established (address={})", address)
        # 購読が生きている間はサーバを維持 (push は _handle 経由)。
        while not lost.is_set():
            await asyncio.sleep(1.0)
    finally:
        if manager is not None:
            try:
                await manager.shutdown()
            except Exception:
                pass
        try:
            await runner.cleanup()
        except Exception:
            pass


# ── トップレベル再接続ループ ─────────────────────────────────────────────────
async def _event_loop(
    runner: Callable[[], Awaitable[None]],
    *,
    reconnect_backoff_sec: float,
) -> None:
    """1 セッションを起動・再起動する無限ループ。cancel で抜ける。"""
    while True:
        try:
            await runner()
        except asyncio.CancelledError:
            logger.info("tapo_event: loop cancelled, exiting")
            raise
        except Exception as e:
            logger.warning("tapo_event: session crashed: {}", e)
        logger.info(
            "tapo_event: session ended, reconnecting in {:.1f}s", reconnect_backoff_sec
        )
        try:
            await asyncio.sleep(reconnect_backoff_sec)
        except asyncio.CancelledError:
            raise


async def _noop_event_loop(reason: str) -> None:
    """依存欠落 / mode=disabled 時の no-op (起動ログだけ出して即終了)。"""
    logger.warning(
        "tapo_event.start_event_subscription: skipped ({}); "
        "Tapo events disabled until configured",
        reason,
    )


# ── 公開 API ─────────────────────────────────────────────────────────────────
async def start_event_subscription(
    on_event: Callable[[TapoEvent], Awaitable[None]],
    *,
    host: str | None = None,
) -> asyncio.Task[None]:
    """Tapo ONVIF イベントを常駐購読し、motion/person を on_event へ流すタスクを起動。

    Args:
        on_event: 1 イベントを受け取る async callback。失敗しても購読は継続する。
        host: (予約) 将来の明示 host 指定用。未使用 (env から取得)。

    Returns:
        起動した `asyncio.Task`。停止は ``task.cancel()``。
        依存未満 / mode=disabled でも即終了する no-op タスクを返す (raise しない)。
    """
    _ = host  # 予約引数 (env 優先)
    mode = _get_event_mode()
    if mode == "disabled":
        return asyncio.create_task(_noop_event_loop("TAPO_EVENT_MODE=disabled"))
    if not _ONVIF_AVAILABLE:
        return asyncio.create_task(
            _noop_event_loop("onvif-zeep-async not installed (camera group)")
        )
    conn_host, user, password, _port = _get_onvif_conn()
    if not conn_host or not user or not password:
        return asyncio.create_task(
            _noop_event_loop("CAMERA_HOST/USERNAME/PASSWORD incomplete")
        )

    debounce_sec = _get_debounce_sec()
    person_only = _get_person_only()
    renew_sec = _get_renew_sec()
    reconnect_backoff_sec = _get_reconnect_backoff_sec()

    if mode == "pullpoint":
        pull_timeout_sec = _get_pull_timeout_sec()

        async def _runner() -> None:
            await _run_one_pullpoint_session(
                on_event,
                debounce_sec=debounce_sec,
                person_only=person_only,
                renew_sec=renew_sec,
                pull_timeout_sec=pull_timeout_sec,
            )

    elif mode == "webhook":
        webhook_host = _get_webhook_host()
        webhook_port = _get_webhook_port()
        if not webhook_host:
            return asyncio.create_task(
                _noop_event_loop("TAPO_EVENT_MODE=webhook but TAPO_EVENT_WEBHOOK_HOST unset")
            )

        async def _runner() -> None:
            await _run_one_webhook_session(
                on_event,
                debounce_sec=debounce_sec,
                person_only=person_only,
                renew_sec=renew_sec,
                webhook_host=webhook_host,
                webhook_port=webhook_port,
            )

    else:
        return asyncio.create_task(_noop_event_loop(f"unknown TAPO_EVENT_MODE={mode!r}"))

    logger.info(
        "tapo_event.start_event_subscription: starting mode={} debounce={:.1f}s "
        "person_only={}",
        mode,
        debounce_sec,
        person_only,
    )
    return asyncio.create_task(
        _event_loop(_runner, reconnect_backoff_sec=reconnect_backoff_sec)
    )
