# core/llm_action.py

import random
import re
from typing import Any, Optional, List

from astrbot.api import logger
from astrbot.core.provider.provider import Provider

from .config import PluginConfig
from .image_gen import ImageGenManager


class LLMAction:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._img_gen_manager = None

    def _get_image_gen_manager(self):
        """懒加载图像生成管理器"""
        if self._img_gen_manager is None:
            self._img_gen_manager = ImageGenManager(self.cfg.image_gen)
        return self._img_gen_manager

    # ========== 图像生成 ==========
    async def generate_images_for_post(
        self,
        text: str,
        reference_image_bytes: Optional[bytes] = None,
    ) -> List[bytes]:
        """为说说生成图片（普通说说）"""
        manager = self._get_image_gen_manager()
        return await manager.generate_for_post(text, reference_image_bytes)

    async def generate_emo_image(
        self,
        text: str,
        reference_image_bytes: Optional[bytes] = None,
    ) -> Optional[bytes]:
        """为emo生成图片"""
        manager = self._get_image_gen_manager()
        return await manager.generate_for_emo(text, reference_image_bytes)

    async def download_file(self, url: str) -> Optional[bytes]:
        """下载文件（用于参考图）"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        logger.error(f"下载文件失败: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"下载文件异常: {e}")
            return None

    # ========== 原有方法 ==========
    def _build_context(
        self, round_messages: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        contexts = []
        for msg in round_messages:
            text_segments = [
                seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"
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
        start = raw.find(start_marker) + len(start_marker)
        end = raw.find(end_marker, start)
        if start != -1 and end != -1:
            return raw[start:end].strip()
        return ""

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
            '- 使用三对双引号（"""）将正文内容包裹起来。\n\n' + prompt_to_use
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
            error_msg = str(e)
            if "sensitive_words_detected" in error_msg:
                raise ValueError("LLM检测到敏感内容，请修改提示词后重试")
            raise ValueError(f"LLM 调用失败：{e}")

    # ========== 生成评论 ==========
    async def generate_comment(self, post) -> str | None:
        provider = self._get_provider(self.cfg.llm.comment_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            logger.error("未配置LLM提供商")
            return None
        try:
            content = post.text
            if post.rt_con:
                content += f"\n[转发]\n{post.rt_con}"

            prompt = f"\n[帖子内容]：\n{content}"

            logger.debug(prompt)
            llm_response = await provider.text_chat(
                system_prompt=self.cfg.llm.comment_prompt,
                prompt=prompt,
                image_urls=post.images,
            )
            comment = re.sub(r"[\s\u3000]+", "", llm_response.completion_text).rstrip("。")
            logger.info(f"LLM 生成的评论：{comment}")
            return comment
        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")

    async def generate_reply(self, post, comment) -> str | None:
        provider = self._get_provider(self.cfg.llm.reply_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            logger.error("未配置LLM提供商")
            return None
        try:
            content = post.text
            if post.rt_con:
                content += f"\n[转发]\n{post.rt_con}"

            prompt = f"\n## 帖子内容\n{content}"
            prompt += f"\n## 要回复的评论\n{comment.nickname}：{comment.content}"
            logger.debug(prompt)
            llm_response = await provider.text_chat(
                system_prompt=self.cfg.llm.reply_prompt, prompt=prompt
            )
            reply = re.sub(r"[\s\u3000]+", "", llm_response.completion_text).rstrip("。")
            logger.info(f"LLM 生成的回复：{reply}")
            return reply
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

        system_prompt = f"""你是一个连载小说作家。
{chr(10).join(context_parts)}

## 本章要求：
- 继续发展故事情节，保持与前文的连贯性。
- 字数：大约 {target_word_count} 字。
- 输出格式：**必须**先用三对双引号 \"\"\" 包裹正文，然后在正文后用 `[摘要]` 标记写一个200字左右的摘要，用于后续参考。
示例：
'''
正文内容...
[摘要] 本章摘要...
'''

请严格按照此格式输出，正文必须用三对双引号包裹。"""

        try:
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                contexts=[],
            )
            full_text = llm_response.completion_text

            logger.info(f"[小说生成] LLM返回内容长度: {len(full_text)}")
            logger.debug(f"[小说生成] LLM原始返回:\n{full_text}")

            if not full_text or not full_text.strip():
                logger.error("[小说生成] LLM返回内容为空")
                raise ValueError("LLM返回内容为空")

            chapter_content = None
            
            content_match = re.search(r'"""(.+?)"""', full_text, re.DOTALL)
            if content_match:
                chapter_content = content_match.group(1).strip()
                logger.info("[小说生成] 使用标准三引号提取正文")
            
            if not chapter_content:
                content_match = re.search(r'"""\s*(.+?)\s*"""', full_text, re.DOTALL)
                if content_match:
                    chapter_content = content_match.group(1).strip()
                    logger.info("[小说生成] 使用宽松三引号提取正文")
            
            if not chapter_content:
                stripped = full_text.strip()
                if not stripped.startswith('[摘要]') and len(stripped) > 100:
                    chapter_content = stripped
                    logger.info("[小说生成] 将整个文本作为正文")
                    
                    prefixes_to_remove = [
                        "这是续写内容：", "以下是正文：", "正文内容：",
                        "继续写：", "接着写：", "接下来：", "然后：",
                        "新章节：", "第", "章："
                    ]
                    for prefix in prefixes_to_remove:
                        if chapter_content.startswith(prefix):
                            chapter_content = chapter_content[len(prefix):].strip()
                            break
            
            if not chapter_content and '[摘要]' in full_text:
                parts = full_text.split('[摘要]', 1)
                if parts[0].strip():
                    chapter_content = parts[0].strip()
                    logger.info("[小说生成] 提取摘要前的内容作为正文")

            if not chapter_content and full_text.strip():
                stripped = full_text.strip()
                if len(stripped) < 50:
                    logger.warning(f"[小说生成] 内容过短: {stripped[:200]}")
                    reject_keywords = ["敏感", "违规", "拒绝", "无法", "不能", "sorry", "cannot"]
                    if any(k in stripped.lower() for k in reject_keywords):
                        raise ValueError(f"LLM拒绝生成：{stripped}")
                    raise ValueError(f"生成内容过短（{len(stripped)}字符）")

            if not chapter_content:
                if full_text.strip().startswith('[摘要]'):
                    raise ValueError("LLM只返回了摘要，未生成正文")
                raise ValueError("生成的小说章节正文为空")

            logger.info(f"[小说生成] 成功提取正文，长度: {len(chapter_content)}")

            summary_match = re.search(r'\[摘要\]\s*(.+?)(?=\[|$)', full_text, re.DOTALL)
            summary = summary_match.group(1).strip() if summary_match else ""

            if not summary:
                summary = await self.summarize_chapter(chapter_content, custom_provider_id)

            return chapter_content, summary
        except Exception as e:
            logger.error(f"[小说生成] 生成失败: {e}")
            raise ValueError(f"LLM 调用失败：{e}")

    async def summarize_chapter(self, chapter_text: str, custom_provider_id: str | None = None) -> str:
        provider = self._get_provider(custom_provider_id or self.cfg.llm.post_provider_id)
        if not provider:
            raise RuntimeError("未配置LLM提供商")

        system_prompt = "请为下面的小说章节写一个200字以内的摘要，只输出摘要内容。"
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

        system_prompt = f"""请将以下内容压缩成一个约 {target_summary_len} 字的摘要。
背景设定：{background_prompt}

待压缩内容：
{chr(10).join(text_to_compress)}

只输出摘要。"""

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

    # ========== 辅助方法 ==========
    def _get_provider(self, provider_id: str | None) -> Provider | None:
        if provider_id:
            return self.cfg.context.get_provider_by_id(provider_id)
        return self.cfg.context.get_using_provider()