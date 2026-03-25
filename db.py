# core/db.py

from __future__ import annotations

import json
import time
from typing import Literal, get_args

import aiosqlite

from .config import PluginConfig
from .model import Comment, Post

# 定义允许查询的字段类型
PostKey = Literal[
    "id",
    "tid",
    "uin",
    "name",
    "gin",
    "status",
    "anon",
    "text",
    "images",
    "videos",
    "create_time",
    "rt_con",
    "comments",
    "extra_text",
]
POST_KEYS = set(get_args(PostKey))  # 若需要，可在文件开头导入 get_args


class PostDB:
    """稿件数据库管理"""

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

    @staticmethod
    def _row_to_post(row) -> Post:
        return Post(
            id=row[0],
            tid=row[1],
            uin=row[2],
            name=row[3],
            gin=row[4],
            text=row[5],
            images=json.loads(row[6]),
            videos=json.loads(row[7]),
            anon=bool(row[8]),
            status=row[9],
            create_time=row[10],
            rt_con=row[11],
            comments=[Comment.model_validate(c) for c in json.loads(row[12])],
            extra_text=row[13],
        )

    @staticmethod
    def _encode_urls(urls: list[str]) -> str:
        return json.dumps(urls, ensure_ascii=False)

    async def initialize(self):
        """初始化数据库（创建所有表）"""
        async with aiosqlite.connect(self.db_path) as db:
            # 稿件表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT UNIQUE,
                    uin INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    gin INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    images TEXT NOT NULL CHECK(json_valid(images)),
                    videos TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(videos)),
                    anon INTEGER NOT NULL CHECK(anon IN (0,1)),
                    status TEXT NOT NULL,
                    create_time INTEGER NOT NULL,
                    rt_con TEXT NOT NULL DEFAULT '',
                    comments TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(comments)),
                    extra_text TEXT
                )
            """)
            # 小说历史表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS novel_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chapter_num INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            # 小说上下文表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS novel_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL DEFAULT '',
                    recent_chapters TEXT NOT NULL DEFAULT '[]',
                    total_chars INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
            """)
            await db.commit()

    # ========== 稿件增删改查 ==========
    async def add(self, post: Post) -> int:
        """添加稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            cur = await db.execute(
                """
                INSERT INTO posts (tid, uin, name, gin, text, images, videos, anon, status, create_time, rt_con, comments, extra_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                ),
            )
            await db.commit()
            last_id = cur.lastrowid
            assert last_id is not None
            return last_id

    async def get(self, value, key: PostKey = "id") -> Post | None:
        """按指定字段查询一条稿件"""
        if value is None:
            raise ValueError("必须提供查询值")
        if key not in POST_KEYS:
            raise ValueError(f"不允许的查询字段: {key}")
        async with aiosqlite.connect(self.db_path) as db:
            if key == "id" and value == -1:
                query = "SELECT * FROM posts ORDER BY id DESC LIMIT 1"
                async with db.execute(query) as cursor:
                    row = await cursor.fetchone()
                    return self._row_to_post(row) if row else None
            query = f"SELECT * FROM posts WHERE {key} = ? LIMIT 1"
            async with db.execute(query, (value,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_post(row) if row else None

    async def list_posts(
        self,
        offset: int = 0,
        limit: int = 1,
        *,
        reverse: bool = False,
    ) -> list[Post]:
        """批量获取稿件（重命名避免与内置 list 冲突）"""
        if offset < 0 or limit <= 0:
            return []

        order = "DESC" if reverse else "ASC"
        async with aiosqlite.connect(self.db_path) as db:
            query = f"""
                SELECT * FROM posts
                ORDER BY id {order}
                LIMIT ? OFFSET ?
            """
            async with db.execute(query, (limit, offset)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_post(row) for row in rows]

    async def update(self, post: Post) -> None:
        """更新稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            await db.execute(
                """
                UPDATE posts SET
                    tid = ?, uin = ?, name = ?, gin = ?, text = ?,
                    images = ?, videos = ?, anon = ?, status = ?,
                    create_time = ?, rt_con = ?, comments = ?, extra_text = ?
                WHERE id = ?
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                    post.id,
                ),
            )
            await db.commit()

    async def save(self, post: Post) -> int | None:
        """保存稿件（自动判断插入或更新）"""
        if post.tid:
            old = await self.get(post.tid, key="tid")
            if old:
                post.id = old.id
                await self.update(post)
                return post.id
        if post.id is not None:
            await self.update(post)
            return post.id
        post.id = await self.add(post)
        return post.id

    async def delete(self, post_id: int) -> int:
        """删除稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            await db.commit()
            return cur.rowcount

    # ========== 小说历史与上下文 ==========
    async def add_novel_chapter(self, chapter_num: int, content: str, summary: str) -> int:
        """添加小说章节（用于追溯）"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO novel_history (chapter_num, content, summary, created_at) VALUES (?, ?, ?, ?)",
                (chapter_num, content, summary, int(time.time()))
            )
            await db.commit()
            return cur.lastrowid

    async def get_last_novel_chapter(self) -> dict | None:
        """获取最新一章（用于追溯）"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT chapter_num, content, summary, created_at FROM novel_history ORDER BY chapter_num DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "chapter_num": row[0],
                        "content": row[1],
                        "summary": row[2],
                        "created_at": row[3],
                    }
                return None

    async def get_novel_context(self) -> dict:
        """获取小说上下文（摘要+最近章节）"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT summary, recent_chapters, total_chars FROM novel_context ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "summary": row[0],
                        "recent_chapters": json.loads(row[1]),
                        "total_chars": row[2],
                    }
                return {"summary": "", "recent_chapters": [], "total_chars": 0}

    async def update_novel_context(self, summary: str, recent_chapters: list[str], total_chars: int):
        """更新小说上下文"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO novel_context (id, summary, recent_chapters, total_chars, updated_at) VALUES (1, ?, ?, ?, ?)",
                (summary, json.dumps(recent_chapters, ensure_ascii=False), total_chars, int(time.time()))
            )
            await db.commit()