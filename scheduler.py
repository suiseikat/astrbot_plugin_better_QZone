# core/scheduler.py

import random
import zoneinfo
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .config import PluginConfig, AutoPublishConfig, EmoModeConfig, AdultModeConfig
from .sender import Sender
from .service import PostService


class BaseTask:
    """任务基类"""
    def __init__(self, name: str, config: PluginConfig):
        self.name = name
        self.config = config
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self.scheduler.start()
        self._terminated = False

    async def schedule_next(self):
        raise NotImplementedError

    async def execute(self):
        raise NotImplementedError

    async def _run_wrapper(self):
        if self._terminated:
            return
        logger.info(f"[{self.name}] 开始执行")
        try:
            await self.execute()
        except Exception as e:
            logger.exception(f"[{self.name}] 执行失败: {e}")
        finally:
            if not self._terminated:
                await self.schedule_next()
            logger.info(f"[{self.name}] 完成")

    def _schedule_at(self, dt: datetime):
        self.scheduler.add_job(
            self._run_wrapper,
            trigger=DateTrigger(run_date=dt, timezone=self.config.timezone),
            name=f"{self.name}_{int(dt.timestamp())}",
            max_instances=1,
        )
        logger.info(f"[{self.name}] 下次执行时间: {dt}")

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        self.scheduler.remove_all_jobs()
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info(f"[{self.name}] 已停止")


class AutoComment(BaseTask):
    """自动评论任务（基于 Cron）"""
    def __init__(self, config: PluginConfig, service: PostService, sender: Sender):
        cron_expr = config.trigger.comment_cron
        if not cron_expr:
            raise ValueError("未配置自动评论 Cron 表达式")
        super().__init__("AutoComment", config)
        self.service = service
        self.sender = sender
        self.trigger = CronTrigger.from_crontab(cron_expr, timezone=config.timezone)
        self._last_base_time = None

    async def schedule_next(self):
        now = datetime.now(self.config.timezone)
        next_time = self.trigger.get_next_fire_time(self._last_base_time, now)
        if not next_time:
            logger.error("[AutoComment] 无法计算下一次执行时间")
            return
        self._last_base_time = next_time

        offset_seconds = self.config.trigger.comment_offset
        if offset_seconds:
            delay = random.randint(-offset_seconds, offset_seconds)
            next_time += timedelta(seconds=delay)
            if next_time <= now:
                next_time = now + timedelta(seconds=1)

        self._schedule_at(next_time)

    async def execute(self):
        try:
            # 获取最新未评论过的说说（最多20条）
            posts = await self.service.query_feeds(
                pos=0,
                num=20,
                no_self=True,
                no_commented=True,
            )
        except Exception as e:
            logger.exception(f"[{self.name}] 查询说说失败: {e}")
            return

        for post in posts:
            try:
                await self.service.comment_posts(post)
                if self.config.trigger.like_when_comment:
                    await self.service.like_posts(post)
            except Exception as e:
                logger.exception(f"[{self.name}] 评论失败: tid={post.tid}, uin={post.uin}, name={post.name}, error={e}")
                continue

            # 通知独立处理，不影响主流程
            try:
                await self.sender.send_admin_post(post, message="定时读说说", reply=False)
            except Exception as e:
                logger.error(f"[{self.name}] 通知发送失败: {e}")


class RandomTimeTask(BaseTask):
    """支持 daily / weekly + range/random 调度的任务"""
    def __init__(
        self,
        name: str,
        config: PluginConfig,
        schedule_type: str,   # daily, weekly
        days_per_week: int,
        time_type: str,       # range, random
        time_range_start: str,
        time_range_end: str,
    ):
        super().__init__(name, config)
        self.schedule_type = schedule_type
        self.days_per_week = days_per_week
        self.time_type = time_type
        self.time_range_start = time_range_start
        self.time_range_end = time_range_end

        self._current_week_days: list[datetime.date] = []
        self._last_week = None

    def _get_next_date(self) -> datetime.date:
        now = datetime.now(self.config.timezone)
        today = now.date()

        if self.schedule_type == "daily":
            # 计算今天的随机时间
            target_time_today = self._get_time_of_day(today)
            if target_time_today <= now:
                # 今天的时间已过，返回明天
                return today + timedelta(days=1)
            return today

        elif self.schedule_type == "weekly":
            week_start = today - timedelta(days=today.weekday())
            if self._last_week != week_start:
                week_days = list(range(7))
                random.shuffle(week_days)
                self._current_week_days = [week_start + timedelta(days=d) for d in week_days[:self.days_per_week]]
                self._current_week_days.sort()
                self._last_week = week_start
                logger.debug(f"[{self.name}] 本周随机日: {[d.strftime('%Y-%m-%d') for d in self._current_week_days]}")

            for d in self._current_week_days:
                if d >= today:
                    return d
            next_week_start = week_start + timedelta(days=7)
            self._last_week = None
            return self._get_next_date()
        else:
            raise ValueError(f"不支持的 schedule_type: {self.schedule_type}")

    def _get_time_of_day(self, date: datetime.date) -> datetime:
        if self.time_type == "random":
            hour = random.randint(0, 23)
            minute = random.randint(0, 59)
            second = random.randint(0, 59)
            dt = datetime.combine(date, datetime.min.time().replace(hour=hour, minute=minute, second=second))
        elif self.time_type == "range":
            start_str = self.time_range_start
            end_str = self.time_range_end
            # 处理 24:00 特殊值（转换为 23:59）
            if end_str == "24:00":
                end_str = "23:59"
            start = datetime.strptime(start_str, "%H:%M").time()
            end = datetime.strptime(end_str, "%H:%M").time()
            seconds = random.randint(
                int(start.hour * 3600 + start.minute * 60),
                int(end.hour * 3600 + end.minute * 60)
            )
            dt = datetime.combine(date, datetime.min.time()) + timedelta(seconds=seconds)
        else:
            raise ValueError(f"不支持的 time_type: {self.time_type}")
        # 添加时区信息，使其与 datetime.now(config.timezone) 一致
        return dt.replace(tzinfo=self.config.timezone)

    async def schedule_next(self):
        next_date = self._get_next_date()
        next_time = self._get_time_of_day(next_date)
        now = datetime.now(self.config.timezone)
        if next_time <= now:
            next_time += timedelta(days=1)
        self._schedule_at(next_time)


class AutoPublishTask(RandomTimeTask):
    def __init__(self, config: PluginConfig, service: PostService, sender: Sender, cfg: AutoPublishConfig):
        super().__init__(
            name="AutoPublish",
            config=config,
            schedule_type=cfg.schedule_type,
            days_per_week=cfg.days_per_week,
            time_type=cfg.time_type,
            time_range_start=cfg.time_range_start,
            time_range_end=cfg.time_range_end,
        )
        self.service = service
        self.sender = sender
        self.llm_provider = cfg.llm_provider_id or None
        self.prompt = cfg.prompt or None

    async def execute(self):
        try:
            text = await self.service.llm.generate_post(
                custom_prompt=self.prompt,
                custom_provider_id=self.llm_provider,
            )
            if not text:
                logger.warning(f"[{self.name}] 生成内容为空，跳过")
                return
            post = await self.service.publish_post(text=text)
        except Exception as e:
            logger.exception(f"[{self.name}] 发布失败: {e}")
            return

        # 通知独立处理
        try:
            await self.sender.send_admin_post(post, message="定时发说说", reply=False)
        except Exception as e:
            logger.error(f"[{self.name}] 通知发送失败: {e}")


class EmoProbTask(BaseTask):
    def __init__(self, config: PluginConfig, service: PostService, sender: Sender, cfg: EmoModeConfig):
        super().__init__("EmoMode", config)
        self.service = service
        self.sender = sender
        self.cfg = cfg
        self.llm_provider = cfg.llm_provider_id or None
        self.prompt = cfg.prompt

    async def schedule_next(self):
        now = datetime.now(self.config.timezone)
        if now.hour == 0 and now.minute == 0:
            await self._check_and_schedule(now)
        else:
            next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            self._schedule_at(next_midnight)

    async def _check_and_schedule(self, now: datetime):
        if random.random() < self.cfg.probability:
            logger.info(f"[{self.name}] 今日触发概率命中，将安排执行")
            target_date = now.date()
            if self.cfg.time_type == "range":
                start_str = self.cfg.time_range_start
                end_str = self.cfg.time_range_end
                if end_str == "24:00":
                    end_str = "23:59"
                start = datetime.strptime(start_str, "%H:%M").time()
                end = datetime.strptime(end_str, "%H:%M").time()
                seconds = random.randint(
                    int(start.hour * 3600 + start.minute * 60),
                    int(end.hour * 3600 + end.minute * 60)
                )
                target_time = datetime.combine(target_date, datetime.min.time()) + timedelta(seconds=seconds)
            else:  # random
                hour = random.randint(0, 23)
                minute = random.randint(0, 59)
                second = random.randint(0, 59)
                target_time = datetime.combine(target_date, datetime.min.time().replace(hour=hour, minute=minute, second=second))
            # 添加时区
            target_time = target_time.replace(tzinfo=self.config.timezone)
            if target_time <= now:
                target_time += timedelta(days=1)
            self._schedule_at(target_time)
        else:
            logger.info(f"[{self.name}] 今日概率未命中，跳过")
            next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            self._schedule_at(next_midnight)

    async def execute(self):
        try:
            text = await self.service.llm.generate_post(
                custom_prompt=self.prompt,
                custom_provider_id=self.llm_provider,
                group_id="",
                topic="",
            )
            if not text:
                logger.warning(f"[{self.name}] 生成内容为空，跳过")
                return
            post = await self.service.publish_post(text=text)
        except Exception as e:
            logger.exception(f"[{self.name}] 发布失败: {e}")
            return

        # 通知独立处理
        try:
            await self.sender.send_admin_post(post, message="emo动态", reply=False)
        except Exception as e:
            logger.error(f"[{self.name}] 通知发送失败: {e}")


class AdultNovelTask(RandomTimeTask):
    def __init__(self, config: PluginConfig, service: PostService, sender: Sender, cfg: AdultModeConfig):
        super().__init__(
            name="AdultNovel",
            config=config,
            schedule_type=cfg.schedule_type,
            days_per_week=cfg.days_per_week,
            time_type=cfg.time_type,
            time_range_start=cfg.time_range_start,
            time_range_end=cfg.time_range_end,
        )
        self.service = service
        self.sender = sender
        self.cfg = cfg
        self.llm_provider = cfg.llm_provider_id or None

    async def execute(self):
        try:
            post = await self.service.publish_novel_chapter()
        except Exception as e:
            logger.exception(f"[{self.name}] 发布失败: {e}")
            return

        # 通知独立处理
        try:
            await self.sender.send_admin_post(post, message="小黄文连载", reply=False)
        except Exception as e:
            logger.error(f"[{self.name}] 通知发送失败: {e}")