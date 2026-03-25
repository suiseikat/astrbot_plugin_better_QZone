# core/service.py

import time
from typing import Any

from astrbot.api import logger

from .db import PostDB
from .llm_action import LLMAction
from .model import Comment, Post
from .qzone import QzoneAPI, QzoneParser, QzoneSession
from .qzone.constants import (
    HTTP_STATUS_FORBIDDEN,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_PERMISSION_DENIED,
    QZONE_CODE_PERMISSION_DENIED_LEGACY,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_INVALID_RESPONSE,
    QZONE_MSG_JSON_PARSE_ERROR,
    QZONE_MSG_NON_OBJECT_RESPONSE,
    QZONE_MSG_PERMISSION_DENIED,
)


class PostService:
    """
    Application Service 层
    """

    def __init__(
        self,
        qzone: QzoneAPI,
        session: QzoneSession,
        db: PostDB,
        llm: LLMAction,
    ):
        self.qzone = qzone
        self.session = session
        self.db = db
        self.llm = llm
        self.sender = None  # 将在 main.py 中设置

    # ========== 统一错误映射 ==========
    def _map_api_error(self, resp, operation: str = "操作") -> str:
        """统一处理 Qzone API 错误"""
        if resp.ok:
            return None
        message = str(resp.message or "").strip()
        code = resp.code

        if code == QZONE_CODE_LOGIN_EXPIRED:
            return "登录状态失效，请重新登录后重试"
        if code in (QZONE_CODE_PERMISSION_DENIED, QZONE_CODE_PERMISSION_DENIED_LEGACY):
            return f"{operation}失败：权限不足"
        if message:
            return f"{operation}失败：{message}"
        return f"{operation}失败：code={code}"

    # ========== 业务接口 ==========

    async def query_feeds(
        self,
        *,
        target_id: str | None = None,
        pos: int = 0,
        num: int = 1,
        with_detail: bool = False,
        no_self: bool = False,
        no_commented: bool = False,
    ) -> list[Post]:
        if target_id:
            resp = await self.qzone.get_feeds(target_id, pos=pos, num=num)
            if not resp.ok:
                raise RuntimeError(self._map_feed_error(resp, target_id=target_id))
            msglist = resp.data.get("msglist") or []
            if not msglist:
                raise RuntimeError(f"QQ {target_id} 暂无可见说说")
            posts: list[Post] = QzoneParser.parse_feeds(msglist)

        else:
            resp = await self.qzone.get_recent_feeds()
            if not resp.ok:
                raise RuntimeError(self._map_feed_error(resp))
            posts: list[Post] = QzoneParser.parse_recent_feeds(resp.data)[
                pos : pos + num
            ]
            if not posts:
                raise RuntimeError("动态流暂无可见说说")

        if no_self:
            uin = await self.session.get_uin()
            posts = [p for p in posts if p.uin != uin]

        if with_detail:
            posts = await self._fill_post_detail(posts)
            if not posts:
                raise RuntimeError("获取详情后无有效说说")

        if no_commented:
            posts = await self._filter_not_commented(posts)

        for post in posts:
            await self.db.save(post)

        return posts

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(k in text for k in keywords)

    def _map_feed_error(self, resp, *, target_id: str | None = None) -> str:
        message = str(resp.message or "").strip()
        lower_message = message.lower()
        code = resp.code
        http_status = self._extract_http_status(resp.raw)

        permission_keywords = (
            "无权限",
            "权限",
            "私密",
            "不可见",
            "拒绝访问",
            "受限",
            "forbidden",
            QZONE_MSG_PERMISSION_DENIED,
            "access denied",
        )
        login_keywords = ("登录", "失效", "skey", "g_tk", "cookie", "expired")

        if code == QZONE_CODE_LOGIN_EXPIRED or self._contains_any(
            lower_message, login_keywords
        ):
            return "登录状态失效，请重新登录后重试"

        if (
            code in (QZONE_CODE_PERMISSION_DENIED, QZONE_CODE_PERMISSION_DENIED_LEGACY)
            or http_status == HTTP_STATUS_FORBIDDEN
            or self._contains_any(lower_message, permission_keywords)
        ):
            if target_id:
                return f"无权限查看 QQ {target_id} 的说说"
            return "无权限访问动态流"

        if code == QZONE_CODE_UNKNOWN and message == QZONE_MSG_EMPTY_RESPONSE:
            if target_id:
                return f"无权限查看 QQ {target_id} 的说说（接口返回空响应）"
            return "动态接口返回空响应，请稍后重试"

        if code == QZONE_CODE_UNKNOWN and message in (
            QZONE_MSG_INVALID_RESPONSE,
            QZONE_MSG_JSON_PARSE_ERROR,
            QZONE_MSG_NON_OBJECT_RESPONSE,
        ):
            return "接口响应格式异常，请稍后重试"

        if message:
            return f"查询说说失败：{message}"
        return f"查询说说失败：code={code}"

    @staticmethod
    def _extract_http_status(raw: dict[str, Any]) -> int | None:
        meta = raw.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            return None
        status = meta.get(QZONE_INTERNAL_HTTP_STATUS_KEY)
        return status if isinstance(status, int) else None

    @staticmethod
    def _has_comment_from_uin(post: Post, uin: int) -> bool:
        return any(comment.uin == uin for comment in post.comments)

    async def _has_saved_self_comment(self, post: Post, uin: int) -> bool:
        if not post.tid:
            return False
        saved_post = await self.db.get(post.tid, key="tid")
        return bool(saved_post and self._has_comment_from_uin(saved_post, uin))

    async def _fill_post_detail(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []

        for post in posts:
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                logger.warning(f"获取详情失败：{resp.data}")
                continue

            parsed = QzoneParser.parse_feeds([resp.data])
            if not parsed:
                logger.warning(f"解析详情失败：{resp.data}")
                continue

            result.append(parsed[0])

        return result

    async def _filter_not_commented(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        uin = await self.session.get_uin()

        for post in posts:
            if self._has_comment_from_uin(post, uin):
                continue
            if await self._has_saved_self_comment(post, uin):
                continue

            if not post.comments:
                resp = await self.qzone.get_detail(post)
                if not resp.ok or not resp.data:
                    continue
                parsed = QzoneParser.parse_feeds([resp.data])
                if not parsed:
                    continue
                post = parsed[0]

            if self._has_comment_from_uin(post, uin):
                continue

            result.append(post)

        return result

    # ==================== 对外接口 ========================

    async def view_visitor(self) -> str:
        """查看访客"""
        resp = await self.qzone.get_visitor()
        if not resp.ok:
            error_msg = self._map_api_error(resp, "获取访客")
            raise RuntimeError(error_msg)
        if not resp.data:
            raise RuntimeError("无访客记录")
        return QzoneParser.parse_visitors(resp.data)

    async def like_posts(self, post: Post):
        """点赞帖子"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        resp = await self.qzone.like(post)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "点赞")
            raise RuntimeError(error_msg)
        logger.info(f"已点赞 → {post.name}")

    async def comment_posts(self, post: Post):
        """评论帖子"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")

        content = await self.llm.generate_comment(post)
        if not content:
            raise ValueError("生成评论内容为空")

        resp = await self.qzone.comment(post, content)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "评论")
            raise RuntimeError(error_msg)

        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                tid=0,
                parent_tid=None,
            )
        )
        await self.db.save(post)
        logger.info(f"评论 → {post.name}")

    async def reply_comment(self, post: Post, index: int):
        """回复评论（自动排除自己的评论）"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")

        uin = await self.session.get_uin()
        other_comments = [c for c in post.comments if c.uin != uin]
        n = len(other_comments)

        if n == 0:
            raise ValueError("没有可回复的评论")

        if not (-n <= index < n):
            raise ValueError(f"索引越界, 当前仅有 {n} 条可回复评论")

        comment = other_comments[index]

        content = await self.llm.generate_reply(post, comment)
        if not content:
            raise ValueError("生成回复内容为空")

        resp = await self.qzone.reply(post, comment, content)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "回复")
            raise RuntimeError(error_msg)

        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                parent_tid=comment.tid,
            )
        )
        await self.db.save(post)

    # ========== 发布方法（集成图像生成） ==========

    async def publish_post(
        self,
        *,
        post: Post | None = None,
        text: str | None = None,
        images: list | None = None,
    ) -> Post:
        """发布说说（支持图像生成）"""
        if post is None and not text and not images:
            raise ValueError("post、text、images 不能同时为空")

        if post is None:
            uin = await self.session.get_uin()
            name = await self.session.get_nickname()
            post = Post(
                uin=uin,
                name=name,
                text=text or "",
                images=images or [],
            )

        cfg = self.session.cfg

        # 图像生成：如果没有手动上传图片且文生图已启用
        if not post.images and cfg.image_gen.text_to_image.enabled:
            # 获取参考图（用于图生图，如果配置了参考图）
            ref_bytes = None
            ref_url = cfg.image_gen.common.reference_image_url
            if ref_url:
                ref_bytes = await self.llm.download_file(ref_url)

            generated_images = await self.llm.generate_images_for_post(
                text=post.text,
                reference_image_bytes=ref_bytes,
            )

            # 保存生成的图片到缓存并添加到 post.images
            for i, img_bytes in enumerate(generated_images):
                img_path = cfg.cache_dir / f"gen_{int(time.time()*1000)}_{i}.png"
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                post.images.append(str(img_path))
                logger.info(f"生成图片已保存: {img_path}")

        # 发布
        resp = await self.qzone.publish(post)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "发布说说")
            raise RuntimeError(error_msg)

        post.tid = resp.data.get("tid")
        post.status = "approved"
        post.create_time = resp.data.get("now", post.create_time)

        await self.db.save(post)
        return post

    async def publish_emo(self) -> Post:
        """发布emo动态（带图像生成）"""
        cfg = self.session.cfg
        emo_cfg = cfg.emo_mode
        if not emo_cfg.enabled:
            raise RuntimeError("emo模式未启用，请在配置中启用")

        provider_id = emo_cfg.llm_provider_id or cfg.llm.post_provider_id
        prompt = emo_cfg.prompt

        text = await self.llm.generate_post(
            group_id="",
            topic="",
            custom_prompt=prompt,
            custom_provider_id=provider_id,
        )
        if not text:
            raise ValueError("生成emo内容为空")

        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        post = Post(
            uin=uin,
            name=name,
            text=text,
            status="approved",
            images=[],
        )

        # emo 图像生成
        if cfg.image_gen.text_to_image.enabled:
            ref_bytes = None
            ref_url = cfg.image_gen.common.reference_image_url
            if ref_url:
                ref_bytes = await self.llm.download_file(ref_url)

            img_bytes = await self.llm.generate_emo_image(
                text=post.text,
                reference_image_bytes=ref_bytes,
            )

            if img_bytes:
                img_path = cfg.cache_dir / f"emo_{int(time.time()*1000)}.png"
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                post.images.append(str(img_path))
                logger.info(f"emo图片已保存: {img_path}")

        # 发布
        resp = await self.qzone.publish(post)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "发布emo动态")
            raise RuntimeError(error_msg)

        post.tid = resp.data.get("tid")
        post.create_time = resp.data.get("now", int(time.time()))
        await self.db.save(post)
        return post

    async def delete_post(self, post: Post):
        """删除帖子"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        resp = await self.qzone.delete(post.tid)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "删除说说")
            raise RuntimeError(error_msg)
        if post.id:
            await self.db.delete(post.id)

    # ========== 小说连载 ==========

    async def publish_novel_chapter(self) -> Post:
        """发布小说新章节（自动管理上下文）"""
        # 获取当前小说上下文
        ctx = await self.db.get_novel_context()
        summary = ctx["summary"]
        recent_chapters = ctx["recent_chapters"]

        # 从配置中获取小说相关设置
        cfg = self.session.cfg
        adult_cfg = cfg.adult_mode

        background_prompt = adult_cfg.prompt
        target_word_count = adult_cfg.target_word_count
        llm_provider = adult_cfg.llm_provider_id or None
        max_chars = adult_cfg.max_history_chars
        keep_recent = adult_cfg.keep_recent_chapters

        # 生成新章节
        chapter_text, chapter_summary = await self.llm.generate_novel_chapter(
            background_prompt=background_prompt,
            context_summary=summary,
            recent_chapters=recent_chapters,
            target_word_count=target_word_count,
            custom_provider_id=llm_provider,
        )

        # 先将正文发送给管理员预览
        logger.info(f"小说章节已生成，长度: {len(chapter_text)}，发送给管理员预览")

        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        preview_post = Post(
            uin=uin,
            name=name,
            text=chapter_text,
            status="approved",
        )

        # 发送给管理员预览（如果 sender 已设置）
        if self.sender:
            try:
                await self.sender.send_admin_post(
                    preview_post,
                    message=f"小黄文新章节预览（共{len(chapter_text)}字）",
                    reply=False
                )
            except Exception as e:
                logger.error(f"发送预览失败: {e}")
        else:
            logger.warning("sender 未设置，无法发送预览")

        # 发布到空间
        post = Post(
            uin=uin,
            name=name,
            text=chapter_text,
            status="approved",
        )
        resp = await self.qzone.publish(post)
        if not resp.ok:
            error_msg = self._map_api_error(resp, "发布小说章节")
            raise RuntimeError(error_msg)

        post.tid = resp.data.get("tid")
        post.create_time = resp.data.get("now", int(time.time()))
        await self.db.save(post)

        # 存储章节到历史表（用于追溯）- 独立处理，不影响主流程
        try:
            last = await self.db.get_last_novel_chapter()
            chapter_num = (last["chapter_num"] + 1) if last else 1
            await self.db.add_novel_chapter(chapter_num, chapter_text, chapter_summary)
        except Exception as e:
            logger.error(f"保存小说历史失败: {e}")

        # 更新上下文 - 独立处理，不影响主流程
        try:
            new_recent = recent_chapters + [chapter_text]
            background_len = len(background_prompt)
            summary_len = len(summary)
            recent_len = sum(len(c) for c in new_recent)
            new_total_chars = background_len + summary_len + recent_len

            if new_total_chars > max_chars and len(new_recent) > keep_recent:
                # 需要压缩
                to_compress_summary = summary
                to_compress_chapters = new_recent[:-keep_recent]
                new_recent = new_recent[-keep_recent:]

                new_summary = await self.llm.compress_history(
                    background_prompt=background_prompt,
                    old_summary=to_compress_summary,
                    old_chapters=to_compress_chapters,
                    custom_provider_id=llm_provider,
                )
                new_total_chars = background_len + len(new_summary) + sum(len(c) for c in new_recent)
            else:
                new_summary = summary

            await self.db.update_novel_context(new_summary, new_recent, new_total_chars)
        except Exception as e:
            logger.error(f"更新小说上下文失败: {e}")

        return post