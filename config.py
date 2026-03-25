# core/config.py

from __future__ import annotations

import zoneinfo
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args, get_origin, get_type_hints, Union

from aiocqhttp import CQHttp

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path


# ========== ConfigNode 基类 ==========
class ConfigNode:
    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, type(None).__class__):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> MappingProxyType:
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ========== 配置子类 ==========
class LLMConfig(ConfigNode):
    post_provider_id: str
    post_prompt: str
    comment_provider_id: str
    comment_prompt: str
    reply_provider_id: str
    reply_prompt: str


class SourceConfig(ConfigNode):
    ignore_groups: list[str]
    ignore_users: list[str]
    post_max_msg: int

    def is_ignore_group(self, group_id: str) -> bool:
        return group_id in self.ignore_groups

    def is_ignore_user(self, user_id: str) -> bool:
        return user_id in self.ignore_users


class TriggerConfig(ConfigNode):
    enabled: bool
    comment_cron: str
    comment_offset: int
    read_prob: float
    send_admin: bool
    like_when_comment: bool


class AutoPublishConfig(ConfigNode):
    enabled: bool
    llm_provider_id: str
    prompt: str
    schedule_type: str
    days_per_week: int
    time_type: str
    time_range_start: str
    time_range_end: str


class EmoModeConfig(ConfigNode):
    enabled: bool
    llm_provider_id: str
    prompt: str
    probability: float
    time_type: str
    time_range_start: str
    time_range_end: str


class AdultModeConfig(ConfigNode):
    enabled: bool
    llm_provider_id: str
    prompt: str
    schedule_type: str
    days_per_week: int
    time_type: str
    time_range_start: str
    time_range_end: str
    target_word_count: int
    max_history_chars: int
    keep_recent_chapters: int


# ========== 图像生成配置 ==========
class TextToImageConfig(ConfigNode):
    enabled: bool
    api_key: str
    model: str
    size: str
    num_inference_steps: int
    cfg_scale: float
    num_images: int
    prompt_suffix: str


class ImageToImageConfig(ConfigNode):
    enabled: bool
    api_key: str
    model: str
    size: str
    num_inference_steps: int
    cfg_scale: float
    return_image_quality: int
    prompt_suffix: str
    reference_images: list[str]          # 新增：参考图文件路径列表
    reference_image_strategy: str        # 新增：多图使用策略 (first/random/round_robin)


class ImageGenCommonConfig(ConfigNode):
    auto_publish_probability: float
    emo_probability: float


class ImageGenConfig(ConfigNode):
    text_to_image: TextToImageConfig
    image_to_image: ImageToImageConfig
    common: ImageGenCommonConfig


class PluginConfig(ConfigNode):
    manage_group: str
    pillowmd_style_dir: str
    llm: LLMConfig
    source: SourceConfig
    trigger: TriggerConfig
    auto_publish: AutoPublishConfig
    emo_mode: EmoModeConfig
    adult_mode: AdultModeConfig
    image_gen: ImageGenConfig
    cookies_str: str
    timeout: int
    show_name: bool

    _DB_VERSION = 4

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_qzone")

        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / f"posts_{self._DB_VERSION}.db"

        self.default_style_dir = (
            Path(get_astrbot_plugin_path()) / "astrbot_plugin_qzone" / "default_style"
        )
        self.style_dir = (
            Path(self.pillowmd_style_dir).resolve()
            if self.pillowmd_style_dir
            else self.default_style_dir
        )

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self._normalize_id()
        self.admin_id = self.admins_id[0] if self.admins_id else None
        self.save_config()

        self.client: CQHttp | None = None

    def _normalize_id(self):
        for ids in [
            self.admins_id,
            self.source.ignore_groups,
            self.source.ignore_users,
        ]:
            normalized = []
            for raw in ids:
                s = str(raw)
                if s.isdigit():
                    normalized.append(s)
            ids.clear()
            ids.extend(normalized)

    def append_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if not self.source.is_ignore_user(uid):
                self.source.ignore_users.append(str(uid))
        self.save_config()

    def remove_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if self.source.is_ignore_user(uid):
                self.source.ignore_users.remove(str(uid))
        self.save_config()

    def update_cookies(self, cookies_str: str):
        self.cookies_str = cookies_str
        self.save_config()