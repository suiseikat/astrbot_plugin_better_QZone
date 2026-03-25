# main.py

import random
import shutil
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.campus_wall import CampusWall
from .core.config import PluginConfig
from .core.db import PostDB
from .core.llm_action import LLMAction
from .core.model import Post
from .core.qzone import QzoneAPI, QzoneSession
from .core.scheduler import AutoComment, AutoPublishTask, EmoProbTask, AdultNovelTask
from .core.sender import Sender
from .core.service import PostService
from .core.utils import get_ats, get_image_urls, parse_range


class QzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 配置
        self.cfg = PluginConfig(config, context)
        # 会话
        self.session = QzoneSession(self.cfg)
        # QQ空间
        self.qzone = QzoneAPI(self.session, self.cfg)
        # 数据库
        self.db = PostDB(self.cfg)
        # LLM模块
        self.llm = LLMAction(self.cfg)
        # 消息发送器
        self.sender = Sender(self.cfg)
        # 操作服务
        self.service = PostService(self.qzone, self.session, self.db, self.llm)
        # 设置 service 的 sender 属性
        self.service.sender = self.sender
        # 表白墙
        self.campus_wall = CampusWall(self.cfg, self.service, self.db, self.sender)
        # 自动评论模块
        self.auto_comment: AutoComment | None = None
        # 自动任务列表
        self.auto_tasks = []

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()

        # 自动评论（保留）
        if self.cfg.trigger.enabled and self.cfg.trigger.comment_cron:
            if not self.auto_comment:
                self.auto_comment = AutoComment(self.cfg, self.service, self.sender)

        # 清理旧任务
        for t in self.auto_tasks:
            await t.terminate()
        self.auto_tasks = []

        # 普通自动发布
        if self.cfg.auto_publish.enabled:
            task = AutoPublishTask(self.cfg, self.service, self.sender, self.cfg.auto_publish)
            self.auto_tasks.append(task)
            await task.schedule_next()

        # emo 模式
        if self.cfg.emo_mode.enabled:
            task = EmoProbTask(self.cfg, self.service, self.sender, self.cfg.emo_mode)
            self.auto_tasks.append(task)
            await task.schedule_next()

        # 小黄文连载
        if self.cfg.adult_mode.enabled:
            task = AdultNovelTask(self.cfg, self.service, self.sender, self.cfg.adult_mode)
            self.auto_tasks.append(task)
            await task.schedule_next()

    async def terminate(self):
        """插件卸载时"""
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        for t in self.auto_tasks:
            await t.terminate()
        if self.cfg.cache_dir.exists():
            try:
                shutil.rmtree(self.cfg.cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def prob_read_feed(self, event: AiocqhttpMessageEvent):
        """监听消息"""
        if not self.cfg.client:
            self.cfg.client = event.bot
            logger.debug("QQ空间所需的 CQHttp 客户端已初始化")

        # 按概率触发点赞+评论
        if not self.cfg.trigger.enabled:
            return

        sender_id = event.get_sender_id()
        if (
            not self.cfg.source.is_ignore_user(sender_id)
            and random.random() < self.cfg.trigger.read_prob
        ):
            target_id = event.get_sender_id()
            try:
                posts = await self.service.query_feeds(
                    target_id=target_id, pos=0, num=1, no_self=True, no_commented=True
                )
            except Exception as e:
                logger.error(f"查询说说失败: {e}")
                return

            for post in posts:
                try:
                    await self.service.comment_posts(post)
                    if self.cfg.trigger.like_when_comment:
                        await self.service.like_posts(post)
                except Exception as e:
                    logger.error(f"评论失败: {e}")
                    continue

                try:
                    await self.sender.send_post(
                        event,
                        post,
                        message="触发读说说",
                        send_admin=self.cfg.trigger.send_admin,
                    )
                except Exception as e:
                    logger.error(f"发送通知失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看访客")
    async def view_visitor(self, event: AiocqhttpMessageEvent):
        """查看访客"""
        try:
            msg = await self.service.view_visitor()
            await self.sender.send_msg(event, msg)
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
        event.stop_event()

    async def _get_posts(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str | None = None,
        with_detail: bool = False,
        no_commented=False,
        no_self=False,
    ) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None

        if target_id:
            self.cfg.remove_ignore_users(target_id)
        try:
            logger.debug(
                f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}"
            )
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=pos,
                num=num,
                with_detail=with_detail,
                no_commented=no_commented,
                no_self=no_self,
            )
            if not posts:
                await event.send(event.plain_result("查询结果为空"))
                event.stop_event()
            return posts
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)
            event.stop_event()
            return []

    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent):
        """
        看说说 <@群友> <序号>
        """
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            await self.sender.send_post(event, post)

    @filter.command("评说说", alias={"评论说说", "读说说"})
    async def comment_feed(self, event: AiocqhttpMessageEvent):
        """评说说 <序号/范围>"""
        posts = await self._get_posts(event, no_commented=True, no_self=True)
        for post in posts:
            try:
                await self.service.comment_posts(post)
                msg = "已评论"
                if self.cfg.trigger.like_when_comment:
                    await self.service.like_posts(post)
                    msg += "并点赞"
                await self.sender.send_post(event, post, message=msg)
            except Exception as e:
                yield event.plain_result(str(e))
                logger.error(e)
                event.stop_event()

    @filter.command("赞说说")
    async def like_feed(self, event: AiocqhttpMessageEvent):
        """赞说说 <序号/范围>"""
        posts = await self._get_posts(event)
        for post in posts:
            try:
                await self.service.like_posts(post)
                await self.sender.send_post(event, post, message="已点赞")
            except Exception as e:
                yield event.plain_result(str(e))
                logger.error(e)
                event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        """发说说 <内容> <图片>, 由用户指定内容"""
        text = event.message_str.partition(" ")[2]
        images = await get_image_urls(event)
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("写说说", alias={"写稿"})
    async def write_feed(self, event: AiocqhttpMessageEvent):
        """写说说 <主题> <图片>, 由AI写完后管理员用‘通过稿件 ID’命令发布"""
        group_id = event.get_group_id()
        topic = event.message_str.partition(" ")[2]
        try:
            text = await self.llm.generate_post(group_id=group_id, topic=topic)
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
            event.stop_event()
            return

        images = await get_image_urls(event)

        # 如果启用了文生图且没有手动上传图片，生成图片
        if not images and self.cfg.image_gen.text_to_image.enabled:
            generated_images = await self.llm.generate_images_for_post(text=text)
            for i, img_bytes in enumerate(generated_images):
                img_path = self.cfg.cache_dir / f"gen_{int(time.time()*1000)}_{i}.png"
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                images.append(str(img_path))
                logger.info(f"生成图片已保存: {img_path}")

        if not text and not images:
            yield event.plain_result("说说生成失败")
            event.stop_event()
            return

        self_id = event.get_self_id()
        post = Post(
            uin=int(self_id),
            text=text or "",
            images=images,
            status="pending",
        )
        await self.db.save(post)
        await self.sender.send_post(event, post)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AiocqhttpMessageEvent):
        """删说说 <稿件ID>"""
        posts = await self._get_posts(event, target_id=event.get_self_id())
        for post in posts:
            try:
                # 先删除，成功后再通知
                await self.service.delete_post(post)
                await self.sender.send_post(event, post, message="已删除说说")
            except Exception as e:
                yield event.plain_result(str(e))
                logger.error(e)
                event.stop_event()

    @filter.command("回评", alias={"回复评论"})
    async def reply_comment(
        self, event: AiocqhttpMessageEvent, post_id: int = -1, comment_index: int = -1
    ):
        """回评 <稿件ID> <评论序号>, 默认回复最后一条非己评论"""
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            event.stop_event()
            return
        try:
            await self.service.reply_comment(post, index=comment_index)
            await self.sender.send_post(event, post, message="已回复评论")
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
        event.stop_event()

    @filter.command("投稿")
    async def contribute_post(self, event: AiocqhttpMessageEvent):
        """投稿 <内容> <图片>"""
        await self.campus_wall.contribute(event)

    @filter.command("匿名投稿")
    async def anon_contribute_post(self, event: AiocqhttpMessageEvent):
        """匿名投稿 <内容> <图片>"""
        await self.campus_wall.contribute(event, anon=True)

    @filter.command("撤稿")
    async def recall_post(self, event: AiocqhttpMessageEvent):
        """删除稿件 <稿件ID>"""
        async for msg in self.campus_wall.delete(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看稿", alias={"查看稿件"})
    async def view_post(self, event: AiocqhttpMessageEvent):
        "查看稿件 <稿件ID>, 默认最新稿件"
        async for msg in self.campus_wall.view(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("过稿", alias={"通过稿件", "通过投稿"})
    async def approve_post(self, event: AiocqhttpMessageEvent):
        """通过稿件 <稿件ID>"""
        async for msg in self.campus_wall.approve(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒稿", alias={"拒绝稿件", "拒绝投稿"})
    async def reject_post(self, event: AiocqhttpMessageEvent):
        """拒绝稿件 <稿件ID> <原因>"""
        async for msg in self.campus_wall.reject(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发emo")
    async def publish_emo(self, event: AiocqhttpMessageEvent):
        """发emo：AI生成一篇emo风格说说并发布（不发送任何通知）"""
        try:
            post = await self.service.publish_emo()
            # 仅记录日志，不发送任何消息
            logger.info(f"已发布emo动态，tid={post.tid}")
        except Exception as e:
            logger.error(f"发布emo失败: {e}")
        finally:
            # 停止事件传播，防止其他插件或框架自动回复
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发小黄文")
    async def publish_adult_chapter(self, event: AiocqhttpMessageEvent):
        """发小黄文：生成并发布新一章连载小说（通知管理员）"""
        try:
            post = await self.service.publish_novel_chapter()
            # 发送给管理员（管理群/管理员）
            await self.sender.send_admin_post(post, message="已发布新章节", reply=False)
            # 私聊通知用户
            await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                message=f"✅ 已发布新章节\n📝 tid={post.tid}\n📄 内容：{post.text[:100]}..."
            )
        except Exception as e:
            await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                message=f"❌ 发布失败：{str(e)}"
            )
            logger.error(e)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置参考图")
    async def set_reference_image(self, event: AiocqhttpMessageEvent):
        """设置参考图（用于图生图）"""
        images = await get_image_urls(event)
        if not images:
            await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                message="请发送一张图片作为参考图"
            )
            event.stop_event()
            return

        # 下载图片并保存
        img_bytes = await self.llm.download_file(images[0])
        if img_bytes:
            # 保存到缓存目录
            ref_path = self.cfg.cache_dir / "reference_image.png"
            with open(ref_path, "wb") as f:
                f.write(img_bytes)
            # 更新配置
            self.cfg.image_gen.image_to_image.reference_images = [str(ref_path)]
            self.cfg.save_config()
            await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                message=f"✅ 参考图已保存\n📁 路径: {ref_path}"
            )
        else:
            await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                message="❌ 下载图片失败"
            )
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清除参考图")
    async def clear_reference_image(self, event: AiocqhttpMessageEvent):
        """清除参考图"""
        self.cfg.image_gen.image_to_image.reference_images = []
        self.cfg.save_config()
        await event.bot.send_private_msg(
            user_id=int(event.get_sender_id()),
            message="✅ 参考图已清除"
        )
        event.stop_event()

    @filter.llm_tool()
    async def llm_view_feed(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
        like: bool = False,
        reply: bool = False,
    ):
        """
        查看、点赞、评论某位用户QQ空间的某条说说、动态
        Args:
            user_id(string): 目标用户的QQ账号，必定为一串数字，如(12345678), 默认为当前用户QQ号
            pos(number): 要查询的说说序号, 默认为0表示最新
            like(boolean): 是否点赞
            reply(boolean): 是否评论
        """
        try:
            user_id = user_id or event.get_sender_id()
            logger.debug(f"正在查询用户（{user_id}）的第 {pos} 条说说")

            posts = await self.service.query_feeds(
                target_id=user_id,
                pos=pos,
                num=1,
                with_detail=True,
            )

            if not posts:
                return "查询结果为空"

            post = posts[0]

            # 执行动作
            msg = ""

            if like and reply:
                await self.service.comment_posts(post)
                await self.service.like_posts(post)
                msg = "已评论并点赞"
            elif reply:
                await self.service.comment_posts(post)
                msg = "已评论"
            elif like:
                await self.service.like_posts(post)
                msg = "已点赞"

            # 发送展示
            await self.sender.send_post(event, post, message=msg)

            return msg + "\n" + post.text + "\n" + "\n".join(post.images)

        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """
        写一篇说说并发布到QQ空间
        Args:
            text(string): 要发布的说说内容
            get_image(boolean): 是否获取当前对话中的图片附加到说说里, 默认为True
        """
        images = await get_image_urls(event) if get_image else []

        # 如果启用了文生图且没有手动上传图片
        if not images and self.cfg.image_gen.text_to_image.enabled:
            generated_images = await self.llm.generate_images_for_post(text=text)
            for i, img_bytes in enumerate(generated_images):
                img_path = self.cfg.cache_dir / f"gen_{int(time.time()*1000)}_{i}.png"
                with open(img_path, "wb") as f:
                    f.write(img_bytes)
                images.append(str(img_path))

        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            return "已发布说说到QQ空间: \n" + post.text + "\n" + "\n".join(post.images)
        except Exception as e:
            return str(e)