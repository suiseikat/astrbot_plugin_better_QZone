# core/image_gen.py

import asyncio
import aiohttp
import random
import time
from typing import Optional, List, Dict, Any

from astrbot.api import logger


class GiteeImageGenerator:
    """Gitee AI 异步图像生成客户端（支持文生图 + 图生图）"""

    def __init__(self, api_key: str, base_url: str = "https://ai.gitee.com/v1"):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {api_key}",
        }

    async def _create_text_task(self, payload: Dict[str, Any]) -> str:
        """创建文生图任务"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/async/images/generations",
                json=payload,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"创建文生图任务失败: {resp.status} - {text}")
                data = await resp.json()
                task_id = data.get("task_id")
                if not task_id:
                    raise ValueError(f"响应中没有 task_id: {data}")
                logger.info(f"创建文生图任务成功，task_id: {task_id}")
                return task_id

    async def _create_edit_task(
        self,
        image_bytes: bytes,
        prompt: str,
        model: str,
        size: str,
        steps: int,
        guidance_scale: float,
        return_image_quality: int = 80,
    ) -> str:
        """创建图生图任务（使用 multipart/form-data）"""
        # 构建 multipart 数据
        boundary = f"----WebKitFormBoundary{random.randint(1000000000, 9999999999)}"
        
        # 手动构建表单数据
        lines = []
        fields = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "steps": str(steps),
            "guidance_scale": str(guidance_scale),
            "return_image_quality": str(return_image_quality),
        }
        for key, value in fields.items():
            lines.append(f'--{boundary}')
            lines.append(f'Content-Disposition: form-data; name="{key}"')
            lines.append('')
            lines.append(value)
        
        # 添加图片文件
        lines.append(f'--{boundary}')
        lines.append('Content-Disposition: form-data; name="image"; filename="reference.png"')
        lines.append('Content-Type: image/png')
        lines.append('')
        
        body = '\r\n'.join(lines).encode() + b'\r\n' + image_bytes + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        
        headers = {
            **self.headers,
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/async/images/edits",
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"创建图生图任务失败: {resp.status} - {text}")
                result = await resp.json()
                task_id = result.get("task_id")
                if not task_id:
                    raise ValueError(f"响应中没有 task_id: {result}")
                logger.info(f"创建图生图任务成功，task_id: {task_id}")
                return task_id

    async def _poll_task(self, task_id: str, timeout: int = 1800, retry_interval: int = 10) -> Dict[str, Any]:
        """轮询任务状态"""
        start_time = time.time()
        max_attempts = int(timeout / retry_interval)
        attempts = 0

        async with aiohttp.ClientSession() as session:
            while attempts < max_attempts:
                attempts += 1
                elapsed = int(time.time() - start_time)
                logger.debug(f"轮询任务状态 [{attempts}], 已等待 {elapsed} 秒...")

                try:
                    async with session.get(
                        f"{self.base_url}/task/{task_id}",
                        headers=self.headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            logger.warning(f"查询任务状态失败: {resp.status} - {text}")
                            await asyncio.sleep(retry_interval)
                            continue

                        result = await resp.json()

                        if result.get("error"):
                            raise Exception(f"{result['error']}: {result.get('message', '未知错误')}")

                        status = result.get("status", "unknown")
                        logger.debug(f"任务状态: {status}")

                        if status == "success":
                            logger.info(f"任务完成，耗时 {elapsed} 秒")
                            return result
                        elif status in ["failed", "cancelled"]:
                            raise Exception(f"任务{status}: {result.get('message', '未知原因')}")
                        await asyncio.sleep(retry_interval)

                except Exception as e:
                    logger.error(f"轮询任务异常: {e}")
                    await asyncio.sleep(retry_interval)

            raise TimeoutError(f"任务超时，已等待 {timeout} 秒")

    async def text_to_image(
        self,
        prompt: str,
        model: str,
        size: str,
        num_inference_steps: int,
        cfg_scale: float,
        num_images: int = 1,
        seed: Optional[int] = None,
    ) -> List[bytes]:
        """文生图"""
        payload = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "num_inference_steps": num_inference_steps,
            "cfg_scale": cfg_scale,
            "n": num_images,
        }
        if seed is not None:
            payload["seed"] = seed

        logger.info(f"文生图: model={model}, size={size}, steps={num_inference_steps}")

        task_id = await self._create_text_task(payload)
        result = await self._poll_task(task_id)

        output = result.get("output", {})
        file_url = output.get("file_url")
        if not file_url:
            raise Exception("任务结果中没有 file_url")

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=60)) as img_resp:
                if img_resp.status == 200:
                    img_bytes = await img_resp.read()
                    logger.info(f"图片下载成功，大小: {len(img_bytes)} bytes")
                    return [img_bytes]
                else:
                    raise Exception(f"下载图片失败: {img_resp.status}")

    async def image_to_image(
        self,
        prompt: str,
        image_bytes: bytes,
        model: str,
        size: str,
        steps: int,
        guidance_scale: float,
        return_image_quality: int = 80,
    ) -> List[bytes]:
        """图生图"""
        logger.info(f"图生图: model={model}, size={size}, steps={steps}, guidance={guidance_scale}")

        task_id = await self._create_edit_task(
            image_bytes=image_bytes,
            prompt=prompt,
            model=model,
            size=size,
            steps=steps,
            guidance_scale=guidance_scale,
            return_image_quality=return_image_quality,
        )
        result = await self._poll_task(task_id)

        output = result.get("output", {})
        file_url = output.get("file_url")
        if not file_url:
            raise Exception("任务结果中没有 file_url")

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=60)) as img_resp:
                if img_resp.status == 200:
                    img_bytes = await img_resp.read()
                    logger.info(f"图片下载成功，大小: {len(img_bytes)} bytes")
                    return [img_bytes]
                else:
                    raise Exception(f"下载图片失败: {img_resp.status}")


class ImageGenManager:
    """图像生成管理器（整合文生图和图生图）"""

    def __init__(self, config):
        """
        :param config: 图像生成配置对象 (ImageGenConfig)
        """
        self.config = config
        self.text_gen = None
        self.image_gen = None
        self._init_generators()
        # 加载参考图
        self.reference_images_bytes = []
        self._load_reference_images()
        self._round_robin_index = 0   # 用于 round_robin 策略

    def _init_generators(self):
        """初始化生成器"""
        if self.config.text_to_image.enabled and self.config.text_to_image.api_key:
            self.text_gen = GiteeImageGenerator(self.config.text_to_image.api_key)
        if self.config.image_to_image.enabled and self.config.image_to_image.api_key:
            self.image_gen = GiteeImageGenerator(self.config.image_to_image.api_key)

    def _load_reference_images(self):
        """从配置加载参考图文件为字节列表"""
        if self.config.image_to_image.enabled:
            for path in self.config.image_to_image.reference_images:
                try:
                    with open(path, 'rb') as f:
                        self.reference_images_bytes.append(f.read())
                    logger.info(f"加载参考图成功: {path}")
                except Exception as e:
                    logger.error(f"加载参考图失败 {path}: {e}")

    def get_reference_image(self) -> Optional[bytes]:
        """
        根据策略获取一张参考图
        策略: first / random / round_robin
        """
        if not self.reference_images_bytes:
            return None
        
        strategy = self.config.image_to_image.reference_image_strategy
        if strategy == "first":
            return self.reference_images_bytes[0]
        elif strategy == "random":
            return random.choice(self.reference_images_bytes)
        elif strategy == "round_robin":
            img = self.reference_images_bytes[self._round_robin_index]
            self._round_robin_index = (self._round_robin_index + 1) % len(self.reference_images_bytes)
            return img
        else:
            # 默认返回第一张
            return self.reference_images_bytes[0]

    async def generate_for_post(
        self,
        text: str,
        reference_image_bytes: Optional[bytes] = None,
    ) -> List[bytes]:
        """
        为说说生成图片（普通说说）
        :param text: 说说文本
        :param reference_image_bytes: 可选的参考图（如果提供，则优先使用）
        :return: 图片字节列表
        """
        # 按概率决定是否生成
        prob = getattr(self.config.common, 'auto_publish_probability', 0.5)
        if random.random() > prob:
            logger.debug("自动发布图片生成概率未命中")
            return []

        # 优先使用传入的参考图，否则从配置中获取
        if reference_image_bytes is None:
            reference_image_bytes = self.get_reference_image()

        # 优先使用图生图（如果有参考图且启用）
        if reference_image_bytes and self.image_gen:
            prompt = text
            if self.config.image_to_image.prompt_suffix:
                prompt = f"{text}, {self.config.image_to_image.prompt_suffix}"
            try:
                return await self.image_gen.image_to_image(
                    prompt=prompt,
                    image_bytes=reference_image_bytes,
                    model=self.config.image_to_image.model,
                    size=self.config.image_to_image.size,
                    steps=self.config.image_to_image.num_inference_steps,
                    guidance_scale=self.config.image_to_image.cfg_scale,
                    return_image_quality=self.config.image_to_image.return_image_quality,
                )
            except Exception as e:
                logger.error(f"图生图失败: {e}")
                # 图生图失败，尝试降级为文生图

        # 文生图
        if self.text_gen:
            prompt = text
            if self.config.text_to_image.prompt_suffix:
                prompt = f"{text}, {self.config.text_to_image.prompt_suffix}"
            try:
                return await self.text_gen.text_to_image(
                    prompt=prompt,
                    model=self.config.text_to_image.model,
                    size=self.config.text_to_image.size,
                    num_inference_steps=self.config.text_to_image.num_inference_steps,
                    cfg_scale=self.config.text_to_image.cfg_scale,
                    num_images=self.config.text_to_image.num_images,
                )
            except Exception as e:
                logger.error(f"文生图失败: {e}")

        return []

    async def generate_for_emo(
        self,
        text: str,
        reference_image_bytes: Optional[bytes] = None,
    ) -> Optional[bytes]:
        """为emo生成单张图片"""
        # 按概率决定是否生成
        prob = getattr(self.config.common, 'emo_probability', 0.3)
        if random.random() > prob:
            logger.debug("emo图片生成概率未命中")
            return None

        # 优先使用传入的参考图，否则从配置中获取
        if reference_image_bytes is None:
            reference_image_bytes = self.get_reference_image()

        # 优先使用图生图（如果有参考图且启用）
        if reference_image_bytes and self.image_gen:
            prompt = text
            if self.config.image_to_image.prompt_suffix:
                prompt = f"{text}, {self.config.image_to_image.prompt_suffix}"
            try:
                images = await self.image_gen.image_to_image(
                    prompt=prompt,
                    image_bytes=reference_image_bytes,
                    model=self.config.image_to_image.model,
                    size=self.config.image_to_image.size,
                    steps=self.config.image_to_image.num_inference_steps,
                    guidance_scale=self.config.image_to_image.cfg_scale,
                    return_image_quality=self.config.image_to_image.return_image_quality,
                )
                return images[0] if images else None
            except Exception as e:
                logger.error(f"emo图生图失败: {e}")

        # 文生图
        if self.text_gen:
            prompt = text
            if self.config.text_to_image.prompt_suffix:
                prompt = f"{text}, {self.config.text_to_image.prompt_suffix}"
            try:
                images = await self.text_gen.text_to_image(
                    prompt=prompt,
                    model=self.config.text_to_image.model,
                    size=self.config.text_to_image.size,
                    num_inference_steps=self.config.text_to_image.num_inference_steps,
                    cfg_scale=self.config.text_to_image.cfg_scale,
                    num_images=1,
                )
                return images[0] if images else None
            except Exception as e:
                logger.error(f"emo文生图失败: {e}")

        return None