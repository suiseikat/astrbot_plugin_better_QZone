# core/llm_action.py
import random
import re
from typing import Any

from astrbot.api import logger
from astrbot.core.provider.provider import Provider

from .config import PluginConfig


class LLMAction:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    # ========== 原有方法 ==========
    def _build_context(
        self, round_messages: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        contexts = []
        for msg in round_messages:
            text_segments = [
                seg["data"]["text"]
                for seg in msg["message"]
                if seg["type"] == "text"
            ]
            text = f"{msg['sender']['nickname']}: {''.join(text_segments).strip()}"
            if text:
                contexts.append({"role": "user", "content": text})
        return contexts

    async def _get_msg_contexts(self, group_id: str) -> list[dict]:
        message_seq = 0
        contexts = []

        if not self.cfg.client:
            raise RuntimeError("客户端未初始化")

        while len(contexts) < self.cfg.source.post_max_msg:
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": 200,
                "reverseOrder": True,
            }
            result: dict = await self.cfg.client.api.call_action(
                "get_group_msg_history", **payloads
            )
            round_messages = result["messages"]
            if not round_messages:
                break

            message_seq = round_messages[0]["message_id"]
            contexts.extend(self._build_context(round_messages))

        return contexts

    @staticmethod
    def extract_content(raw: str) -> str:
        start_marker = '"""'
        end_marker = '"""'
        start = raw.find(start_marker)
        if start == -1:
            return ""
        start += len(start_marker)
        end = raw.find(end_marker, start)
        if end == -1:
            return ""
        return raw[start:end].strip()

    # ========== 辅助方法 ==========
    def _get_provider(self, provider_id: str | None) -> Provider | None:
        if provider_id:
            return self.cfg.context.get_provider_by_id(provider_id)
        return self.cfg.context.get_using_provider()

    # ========== 生成普通说说 ==========
    async def generate_post(
        self,
        group_id: str = "",
        topic: str | None = None,
        custom_prompt: str | None = None,
        custom_provider_id: str | None = None,
        target_word_count: int | None = None,
    ) -> str | None:
        provider = self._get_provider(custom_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            raise RuntimeError("未配置LLM提供商")
        if not self.cfg.client:
            raise RuntimeError("客户端未初始化")

        if group_id:
            contexts = await self._get_msg_contexts(group_id)
        else:
            group_list = await self.cfg.client.get_group_list()
            group_ids = [
                str(group["group_id"])
                for group in group_list
                if str(group["group_id"]) not in self.cfg.source.ignore_groups
            ]
            if not group_ids:
                logger.warning("未找到可用群组")
                return None
            group_id = random.choice(group_ids)
            contexts = await self._get_msg_contexts(group_id)

        prompt_to_use = custom_prompt if custom_prompt else self.cfg.llm.post_prompt
        system_prompt = (
            f"# 写作主题：{topic or '从聊天内容中选一个主题'}\n\n"
            "# 输出格式要求：\n"
            '- 使用三对双引号（"""）将正文内容包裹起来。\n\n'
            + prompt_to_use
        )

        if target_word_count:
            system_prompt += f"\n\n# 字数要求：大约{target_word_count}字。"

        logger.debug(f"{system_prompt}\n\n{contexts}")

        try:
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                contexts=contexts,
            )
            diary = self.extract_content(llm_response.completion_text)
            if not diary:
                raise ValueError("LLM 生成的日记为空")
            logger.info(f"LLM 生成的日记：{diary[:100]}...")
            return diary
        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")

    # ========== 小说连载相关 ==========
    async def generate_novel_chapter(
        self,
        background_prompt: str,
        context_summary: str,
        recent_chapters: list[str],
        target_word_count: int,
        custom_provider_id: str | None = None,
    ) -> tuple[str, str]:
        provider = self._get_provider(custom_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            raise RuntimeError("未配置LLM提供商")

        context_parts = [background_prompt]
        if context_summary:
            context_parts.append(f"【历史摘要】\n{context_summary}")
        if recent_chapters:
            context_parts.append("【最近几章内容】\n" + "\n---\n".join(recent_chapters))

        system_prompt = (
            f"你是一个连载小说作家。\n"
            f"{chr(10).join(context_parts)}\n\n"
            f"## 本章要求：\n"
            f"- 继续发展故事情节，保持与前文的连贯性。\n"
            f"- 字数：大约 {target_word_count} 字。\n"
            f"- 输出格式：先用三对双引号包裹正文，然后在正文后用 `[摘要]` 标记写一个200字左右的摘要，用于后续参考。\n\n"
            f"示例：\n"
            f"\"\"\"\n"
            f"正文内容...\n"
            f"\"\"\"\n"
            f"[摘要] 本章摘要...\n"
            f"请严格按照此格式输出。"
        )

        try:
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                contexts=[],
            )
            full_text = llm_response.completion_text

            content_match = re.search(r'"""(.+?)"""', full_text, re.DOTALL)
            if not content_match:
                raise ValueError("未找到正文")

            chapter_content = content_match.group(1).strip()

            # 匹配 [摘要] 到下一个 [ 或结尾
            summary_match = re.search(r'\[摘要\]\s*(.+?)(?=\[|$)', full_text, re.DOTALL)
            summary = summary_match.group(1).strip() if summary_match else ""

            if not summary:
                summary = await self.summarize_chapter(chapter_content, custom_provider_id)

            return chapter_content, summary

        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")

    async def summarize_chapter(
        self,
        chapter_text: str,
        custom_provider_id: str | None = None,
    ) -> str:
        provider = self._get_provider(custom_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            raise RuntimeError("未配置LLM提供商")

        system_prompt = "请为下面的小说章节写一个200字以内的摘要，概括主要情节和人物发展。只输出摘要内容。"

        try:
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=chapter_text,
            )
            summary = llm_response.completion_text.strip()
            return summary[:300]
        except Exception as e:
            logger.error(f"摘要生成失败：{e}")
            return "摘要生成失败"

    async def compress_history(
        self,
        background_prompt: str,
        old_summary: str,
        old_chapters: list[str],
        target_summary_len: int = 500,
        custom_provider_id: str | None = None,
    ) -> str:
        provider = self._get_provider(custom_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            raise RuntimeError("未配置LLM提供商")

        text_to_compress = []
        if old_summary:
            text_to_compress.append(f"旧摘要：{old_summary}")
        if old_chapters:
            text_to_compress.append("旧章节内容：\n" + "\n---\n".join(old_chapters))

        system_prompt = f"""你是小说助手，请将以下内容压缩成一个约 {target_summary_len} 字的摘要，要求保留主要情节、人物关系和关键冲突，风格与背景设定保持一致。
背景设定：{background_prompt}
待压缩内容：
{chr(10).join(text_to_compress)}
只输出摘要，不要其他内容。"""

        try:
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                contexts=[],
            )
            summary = llm_response.completion_text.strip()
            if len(summary) > target_summary_len * 1.2:
                summary = summary[:target_summary_len] + "…"
            return summary
        except Exception as e:
            logger.error(f"压缩历史失败：{e}")
            return old_summary or "（历史摘要生成失败）"
