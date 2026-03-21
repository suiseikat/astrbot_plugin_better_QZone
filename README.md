# astrbot_plugin_better_Qzone

_✨ [astrbot](https://github.com/AstrBotDevs/AstrBot) QQ空间对接插件 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

## 🤝 介绍

QQ空间对接插件，可自动发说说、表白墙投稿审核、查看说说、点赞、评论等。  
新增**多种自动发布模式**（普通/emo/小黄文连载）、**AI小说连载历史压缩**、**手动触发指定类型发布**等功能。

## 📦 安装

- 直接在 AstrBot 的插件市场搜索 `astrbot_plugin_qzone`，点击安装，等待完成即可。

- 也可克隆源码到插件文件夹：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/Zhalslar/astrbot_plugin_qzone
# 重启 AstrBot
```

## ⌨️ 配置

请前往插件配置面板进行配置，主要分为以下几类：

### 1. 自动发布模式

#### 普通说说自动发布 (`auto_publish`)
- **启用/禁用**：`enabled`
- **LLM 提供商**：可独立选择，留空则使用 `llm.post_provider_id`
- **自定义提示词**：留空则使用 `llm.post_prompt`
- **发布频率**：
  - `daily`：每天发布一次
  - `weekly`：每周随机选择 N 天发布（`days_per_week` 设置天数）
- **时间范围**：
  - `range`：在 `time_range_start` ~ `time_range_end` 之间随机选择时间
  - `random`：全天任意时间随机

#### emo 动态 (`emo_mode`)
- **启用/禁用**：`enabled`
- **LLM 提供商**：同上
- **自定义提示词**：可单独编写 emo 风格文案
- **触发概率**：每天以 `probability` 概率决定是否触发
- **时间范围**：同普通模式，建议设定在深夜（如 `00:00` ~ `06:00`）

#### 小黄文连载 (`adult_mode`)
- **启用/禁用**：`enabled`
- **LLM 提供商**：同上
- **背景设定**：`prompt` 作为固定背景，永不压缩
- **发布频率**：同普通模式
- **时间范围**：同普通模式
- **目标字数**：每次生成约 `target_word_count` 字
- **历史管理**：
  - `max_history_chars`：累积上下文最大字符数（背景+摘要+最近章节），超过自动压缩
  - `keep_recent_chapters`：压缩时保留的最近章节数量（全文）

### 2. 自动评论与回复 (`trigger`)
- **总开关** `enabled`：关闭后所有自动评论均不触发
- **定时评论**：`comment_cron` + `comment_offset` 决定定时扫描好友动态并评论
- **聊天触发**：`read_prob` 控制用户在群聊/私聊时随机评论对方最新说说的概率
- **通知方式**：`send_admin` 决定评论结果仅通知管理员还是发到群聊
- **联动点赞**：`like_when_comment` 决定评论时是否自动点赞

### 3. 其他配置
- **管理群**：`manage_group`，投稿审批群（不填则私发管理员）
- **Pillowmd 样式目录**：渲染说说图片的样式
- **LLM 模块**：手动发说说、评论、回复的默认提供商和提示词
- **忽略列表**：`source.ignore_groups` / `source.ignore_users`
- **Cookie**：`cookies_str`（留空则从 CQHTTP 自动获取）

## 🐔 使用说明（QzonePlugin）

### 一、基础说明

- **默认查看的是“好友动态流”**
- **@某人 / @QQ号**：表示查看该用户的 QQ 空间
- **序号从 0 开始**
  - `0` = 最新一条
  - `-1` = 最后一条
- 支持 **范围语法**：`2~5`
- 机器人在需要评论 / 回复时，会 **自动排除自己的评论**

### 二、命令一览表

| 命令 | 别名 | 权限 | 参数 | 功能说明 |
|------|------|------|------|----------|
| 查看访客 | - | ADMIN | - | 查看 QQ 空间最近访客列表 |
| 看说说 | 查看说说 | ALL | `[@用户] [序号/范围]` | 查看说说（自动拉取完整详情） |
| 评说说 | 评论说说、读说说 | ALL | `[@用户] [序号/范围]` | 给说说评论（可配置自动点赞） |
| 赞说说 | - | ALL | `[@用户] [序号/范围]` | 给说说点赞 |
| 发说说 | - | ADMIN | `<文本> [图片]` | 立即发布一条说说 |
| 写说说 | 写稿 | ADMIN | `<主题> [图片]` | AI 生成草稿，保存为待审核稿件 |
| **发emo** | - | ADMIN | - | 立即用 emo 模式生成并发布一条动态 |
| **发小黄文** | - | ADMIN | - | 立即生成并发布新一章小黄文连载 |
| 删说说 | - | ADMIN | `<序号>` | 删除自己发布的说说 |
| 回评 | 回复评论 | ALL | `<稿件ID> [评论序号]` | 回复评论（默认回复最后一条非自己评论） |
| 投稿 | - | ALL | `<文本> [图片]` | 向表白墙投稿 |
| 匿名投稿 | - | ALL | `<文本> [图片]` | 匿名投稿到表白墙 |
| 看稿 | 查看稿件 | ADMIN | `[稿件ID]` | 查看稿件（默认最新） |
| 过稿 | 通过稿件、通过投稿 | ADMIN | `<稿件ID>` | 审核并发布稿件 |
| 拒稿 | 拒绝稿件、拒绝投稿 | ADMIN | `<稿件ID> [原因]` | 拒绝稿件 |
| 撤稿 | - | ALL | `<稿件ID>` | 撤回自己投稿的稿件 |

### 三、范围参数使用示例

```text
看说说
看说说 2
看说说 1~3
看说说 @某人
看说说 @某人 0
```

## 💡 TODO

- [x] 发说说
- [x] 校园表白墙功能：投稿、审核投稿
- [x] 点赞说说（接口显示成功，但实测点赞无效）
- [x] 评论说说
- [x] 定时自动发说说、日记
- [x] 定时自动评论、点赞好友的说说
- [x] LLM发说说
- [x] **多种自动发布模式（普通/emo/小黄文）**
- [x] **小黄文连载与历史压缩**
- [ ] LLM配图
- [ ] 自动上网冲浪写说说

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）

## 🤝 鸣谢

- 部分代码参考了[CampuxBot项目](https://github.com/idoknow/CampuxBot)，由作者之一的Soulter推荐

- [QQ 空间爬虫之爬取说说](https://kylingit.com/blog/qq-空间爬虫之爬取说说/)
  感谢这篇博客提供的思路。

- [一个QQ空间爬虫项目](https://github.com/wwwpf/QzoneExporter)

- [QQ空间](https://qzone.qq.com/) 网页显示本地数据时使用的样式与布局均来自于QQ空间。
```
