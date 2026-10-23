# 群助手（astrbot_plugin_groupkit）

AstrBot v4 插件 · 仅 OneBot v11（NapCat）
信息卡 / 额度与抽奖 / 发言统计 / 真实入群天数 / 基础群管。

主打：**不用注册、口令是白话、输出带 emoji 分段、不刷屏。**

---

## 安装（Ubuntu 宿主机 / Docker）

### 1. 把插件目录放到 `data/plugins/`

解压后应该是这一层（`main.py` 必须直接躺在插件目录根下，不能多套一层）：

```
AstrBot/data/plugins/astrbot_plugin_groupkit/main.py
```

如果服务器上没有 `unzip`：

```bash
sudo apt update && sudo apt install -y unzip
unzip astrbot_plugin_groupkit.zip -d /path/to/AstrBot/data/plugins/
# 或者不装 unzip：
python3 -m zipfile -e astrbot_plugin_groupkit.zip /path/to/AstrBot/data/plugins/
```

**源码部署**：`data/plugins/` 就在 AstrBot 项目目录下，通常是 `~/AstrBot/data/plugins/`。

**Docker 部署**：

`data/plugins/` 是宿主机挂进容器的那个目录（compose 里 `volumes: - ./data:/AstrBot/data`）。
先确认映射关系：

```bash
docker ps --format '{{.Names}}\t{{.Image}}'
docker inspect <容器名> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

找到映射到 `/AstrBot/data` 的宿主机目录，把插件解压到它的 `plugins/` 下：

```bash
unzip /tmp/astrbot_plugin_groupkit.zip -d /path/to/data/plugins/
docker exec -it <容器名> ls /AstrBot/data/plugins/astrbot_plugin_groupkit
```

也可以用 `docker cp`（`/AstrBot/data` 是挂载卷，所以拷进去的文件同样持久）：

```bash
docker cp /tmp/astrbot_plugin_groupkit <容器名>:/AstrBot/data/plugins/
```

### 2. 装依赖（Pillow）

先检查镜像里是不是已经有，有就不用装：

```bash
docker exec -it <容器名> python -c "import PIL; print(PIL.__version__)"
```

缺的话：

```bash
docker exec -it <容器名> pip install Pillow
```

> ⚠️ **容器里 pip 装的东西在容器可写层，`docker compose down` 后重建会丢。**
> 插件文件放在 data 卷里不会丢，但 Pillow 会。永久化的做法是把依赖烧进镜像：

```dockerfile
# 在 docker-compose.yml 同目录建 Dockerfile
FROM soulter/astrbot:latest
RUN pip install Pillow
```

```yaml
# docker-compose.yml 里把 image 换成 build
services:
  astrbot:
    build: .
    # image: soulter/astrbot:latest   ← 注释掉这行
```

然后 `docker compose up -d --build`。以后重建容器 Pillow 都在。

### 3. 重载插件

AstrBot WebUI → 插件 → 找到「群助手」→ 点卡片上的刷新图标（重载插件）。

日志出现下面这行即成功：

```
[群助手] 已加载，数据目录：/.../data/plugin_data/astrbot_plugin_groupkit
```

### 4. 配置

插件配置里填 `enabled_group_ids`（**留空 = 机器人在的所有群都响应**，测试期建议先填目标群号）。

---

## 安装排错

| 日志 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'PIL'` | 没装 Pillow，或装到了别的 Python 里 | 用 AstrBot 那个解释器的 pip 重装 |
| `ModuleNotFoundError: ... core / utils` | 目录套多了一层，或缺文件 | 确认 `main.py` 和 `core/` 同级 |
| `Permission denied`（写 database） | 插件目录或 `plugin_data` 不可写 | `sudo chown -R $(whoami)` 对应目录 |
| 信息卡显示成纯文字 | 画图失败（字体/背景读不到） | 看日志里的「渲染信息卡失败」，插件会自动降级成文字卡 |

验证结构：

```bash
find data/plugins/astrbot_plugin_groupkit -maxdepth 2 -type f | sort
```

应该能看到 `main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt`、
`core/`、`utils/`、`assets/`（含 `card_bg.png` 和字体）。

---

## 没有注册这一步

谁在群里说话，谁就自动有档案：

- **显示名**：优先用你 `改名` 设置的，否则用 QQ 群名片 / 昵称，最后才用 QQ 号。
  **插件不写 QQ 群名片**，两者互不干扰（详见下面「改名 vs QQ 群名片」）。
- **性别**：直接取 QQ 资料（`get_group_member_info` 的 `sex`），不让你手填。
- **在册天数**：取**真实入群时间**（见下）。
- **新人礼**：第一次发言自动到账 5 额度，只发一次。

### 改名 vs QQ 群名片

这两件事**完全独立**：

| | 谁能改 | 改了之后影响 |
|---|---|---|
| QQ 群名片 | 群友自己在 QQ 里改（群主可关闭此权限），或管理员代改 | 只影响 QQ 里显示的名字 |
| 插件显示名（`改名`） | 群友自己，或 2 级以上的管理代改 | **只影响信息卡和以后的榜单显示，不动 QQ 名片** |

因为独立，所以存在两种用法，看你怎么定：

- 想让卡片名字和 QQ 名片一致 → 干脆别用 `改名`，卡片会自动跟着 QQ 名片走。
- 想让卡片上有个"机器人专属昵称" → 用 `改名`，它不会被别人在 QQ 里改名片时冲掉。

---

## 真实入群时间是怎么来的

两条来源，互为补充：

1. **协议端查询**：NapCat 的 `get_group_member_info` 会返回 `join_time`（unix 秒）。
   渲染信息卡时如果发现这个成员还没有 `join_time`，就查一次并缓存进库。
   所以**历史成员也能拿到真实入群时间**，不需要等通知事件。
2. **入群通知**：实时收到 `group_increase` 通知时直接记下时间戳，新成员立刻准确。

查不到（接口失败 / 协议端不返回）时才退化成「本插件第一次见到他的日期」。

在册天数 = 今天 − 入群日期 + 1，最少 1 天。

---

## 口令

口令**不需要任何前缀**，直接说话即可。

### 所有人（1 级）

| 口令 | 说明 |
|---|---|
| `i` | 个人信息卡（图片） |
| `i @某人` / `i 123456789` | 看别人的信息卡 |
| `改名 李四` | 改信息卡显示名，消耗 1 额度 |
| `爱好 打游戏` | 设置爱好 |
| `额度` | 余额 + 抽奖进度 |
| `抽奖` | 消耗一次机会，随机 1~5 额度（10% 暴击 +8） |
| `今日` | 今日 / 本月 / 累计发言 |
| `帮助` | 菜单（合并转发，点开才展开） |

### 2 级 · 助手

| 口令 | 说明 |
|---|---|
| `禁言 @某人 10分钟` | 默认单位分钟，最长 30 天 |
| `解禁 @某人` | |
| `改名 @某人 李四` | 代改他人显示名，不扣对方额度 |

### 3 级 · 管理

| 口令 | 说明 |
|---|---|
| `踢 @某人` | |
| `发额度 @某人 50` | |
| `扣额度 @某人 50` | 不会扣成负数 |

### 4 级 · 群主

| 口令 | 说明 |
|---|---|
| `全群禁言` / `全群解禁` | |
| `授权 @某人 3` | 设置插件权限 1~4 |

### 权限怎么算

```
实际等级 = max(QQ 群角色映射, 插件内授权)
QQ 群主 → 4 级；QQ 管理员 → 3 级；普通成员 → 1 级
```

**授权不看身份，只看等级**：群主可以把任何人授到 4 级，那个人之后就同样拥有
4 级权限 —— 包括继续授权别人、开全群禁言。所以「授权」不专属于 QQ 群主。

取 `max` 是为了防止手动授权把群主/管理员降权。

---

## 额度怎么来怎么去

- **来**：新人礼 20；每发言 100 条攒 1 次抽奖机会，`抽奖` 得 1~5（10% 概率暴击 +8）。
- **去**：改名花 10。
- 所有变动写 `quota_logs` 表，可审计。

额度是为后续高阶功能（图卡、AI 对话、小游戏、骰子）预留的通用货币，
加新的消耗点只要调 `db.add_quota(..., -cost, "原因")`。

---

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled_group_ids` | `[]` | 留空 = 所有群 |
| `currency_name` | `额度` | 货币叫什么 |
| `welcome_quota` | `5` | 新人礼，只发一次 |
| `rename_cost` | `1` | 改名消耗 |
| `draw_per` | `100` | 多少条发言换一次抽奖 |
| `draw_min` / `draw_max` | `1` / `5` | 抽奖区间 |
| `draw_bonus_chance` / `draw_bonus_amount` | `0.1` / `8` | 暴击概率与额度 |
| `notify_draw_ready` | `true` | 攒满时在群里提示一次 |
| `count_command_messages` | `false` | 口令是否计入发言 |
| `info_triggers` | `i,我的信息,名片` | 信息卡触发词，逗号分隔 |
| `help_style` | `forward` | 帮助用合并转发还是纯文字 |
| `card_bg_path` | `assets/card_bg.png` | 信息卡背景，换成立绘即可 |
| `card_font_path` | `assets/fonts/...ttf` | 字体 |
| `card_show_qq` | `false` | 卡上是否显示 QQ 号 |
| `card_show_avatar` | `true` | 右侧是否显示 QQ 头像（拉不到自动用名字首字占位） |
| `log_retention_days` | `180` | 流水/日志保留天数，0 = 永久 |
| `stats_retention_days` | `730` | 按天统计保留天数，0 = 永久 |
| `owner_default_level` / `admin_default_level` | `4` / `3` | 群角色映射出的权限 |

---

## 换皮：个人信息卡

信息卡是 1200×680 的图，左侧信息板由代码绘制，右侧留空给背景立绘。

- 直接把 `assets/card_bg.png` 换成自己的图（同尺寸）即可，**不用改代码**。
- 调版式改 `utils/card.py` 顶部常量：`PANEL` / `ROWS_TOP` / `ROW_GAP` / 各颜色。
- 看当前效果：`python tools/selftest.py` → `tools/out/card_preview.png`。
- 想看**不同人**的效果（新人 / 老成员 / 管理员，含超长名字、超长爱好、六位数额度）：
  `python tools/preview_cards.py` → `tools/out/card_*.png`。
- 重新生成占位背景：`python tools/make_bg.py`。

卡片是**每次调用现画**的：`main.py` 的 `_card_reply()` 按 `(group_id, user_id)` 查库，
经 `services.py` 的 `card_data()` 组装成 dict，再交给 `CardRenderer.render(data)` 渲染。
同一个渲染器服务所有人，数据不同图就不同。

排版对数据量做了自适应：姓名过长先缩字号再截断加「…」，数值过长同样处理，
行数变化（`card_show_qq` 会多一行）时行距自动压缩，不会顶出面板。

字体是霞鹜文楷（LXGW WenKai，OFL-1.1 授权），已随插件附带，也可换成任意 ttf。
QQ 名片里的 emoji 会被自动过滤（图片字体画不出来，会变豆腐块），名字过长会自动缩字号。

左列的小图标是**代码画的矢量图形**（人像 / 爱心 / 圆币 / 日历 / 时钟 / 柱状 / 星），
不用文字也不用 emoji —— 一是竖着读一列汉字容易读出别的词，二是 emoji 在图片里渲染不了。

---

## 数据层（为长期运行准备）

数据在 `data/plugin_data/astrbot_plugin_groupkit/groupkit.db`（符合 AstrBot 规范，
插件更新不覆盖）。单文件 SQLite，开启 WAL，读写不互堵。

### 表结构

| 表 | 主键 | 说明 | 增长量级 |
|---|---|---|---|
| `members` | `(group_id, user_id)` | 一人一行：显示名、性别、爱好、额度、累计发言、抽奖进度、权限、真实入群时间 | 群数 × 人数，有界 |
| `daily_stats` | `(group_id, user_id, stat_date)` | 一人一天一行，只用于算今日/本月 | 人数 × 天数，可裁剪 |
| `quota_logs` | 自增 `id` | 额度流水，只增不改，可审计 | 可裁剪 |
| `op_logs` | 自增 `id` | 群管操作审计（禁言/踢人/授权…） | 可裁剪 |

- **累计发言数存在 `members.total_messages`**，不依赖明细表，所以裁剪 `daily_stats` 不会让累计数变少。
- 时间字段统一：`join_time` 存 unix 秒（便于范围查询），`created_at` / `stat_date` 存
  `YYYY-MM-DD HH:MM:SS` / `YYYY-MM-DD`（人读友好，且字符串排序即时间排序）。

### 索引

按真实查询模式建，不做全表扫：

```
idx_daily_user_date      (group_id, user_id, stat_date)   今日/本月
idx_daily_group_date     (group_id, stat_date)            群维日报
idx_quota_user           (group_id, user_id, id)          按人查流水
idx_quota_time           (created_at)                     按时间裁剪
idx_op_time              (created_at)                     按时间裁剪
idx_members_quota        (group_id, quota DESC)           排行榜预留
idx_members_messages     (group_id, total_messages DESC)  排行榜预留
```

### 并发与一致性

- `isolation_level=None` + 显式 `BEGIN IMMEDIATE ... COMMIT`：所有多语句写操作（发新人礼、
  发言计数+抽奖结算、扣额度+记流水）都在一个事务里，**中途异常整体回滚**，
  不会出现「扣了钱没流水」或「加了机会没记账」。
- 进程内 `RLock` 串行化本插件的写操作；`busy_timeout=5000` 兜住跨进程冲突。
- `synchronous=NORMAL` + WAL：在掉电风险和写入速度之间取均衡，是 SQLite 推荐的组合。
- 结构版本写进 `PRAGMA user_version`，配合「列是否存在」检查做可重复迁移，
  以后加字段直接往 `_migrate()` 里加一行，老库自动升级。

### 保留期清理

每天最多自动清一次（`_maybe_prune`），两个配置控制：

| 配置 | 默认 | 说明 |
|---|---|---|
| `log_retention_days` | 180 | 额度流水 + 操作日志保留天数，0 = 永久 |
| `stats_retention_days` | 730 | 按天发言统计保留天数，0 = 永久 |

裁剪只动明细，不动 `members` 上的累计值。

### 备份

`db.backup_to(path)` 是 SQLite 在线备份（不锁库），需要定时归档时直接调它。

---

## 目录结构

```
astrbot_plugin_groupkit/
├── main.py                 # 事件入口 + 口令路由 + 群管
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── core/
│   ├── config.py           # 配置强类型封装（全部带兜底）
│   ├── database.py         # SQLite：members / daily_stats / quota_logs / op_logs
│   └── services.py         # 权限 / 资料 / 统计 / 额度，返回文本不直接发
├── utils/
│   ├── router.py           # compact / strip_at / drop_prefix / parse_duration…
│   ├── onebot.py           # call_action 封装，失败只记日志不抛
│   └── card.py             # 信息卡渲染（含 emoji 过滤）
├── assets/                 # 背景图 + 字体
└── tools/
    ├── make_bg.py          # 生成占位背景
    └── selftest.py         # 不启动 AstrBot 的本地自测
```

数据在 `data/plugin_data/astrbot_plugin_groupkit/groupkit.db`（符合 AstrBot 规范，
插件更新不会覆盖）。信息卡临时图在 `.../cards/`，一小时自动清理。

---

## 本地自测

```bash
python tools/selftest.py
```

用桩替换 `astrbot.*`，跑 60 项断言：口令解析、配置兜底、抽奖进度数学、
新人礼只发一次、余额不足拒绝扣款、事务异常整体回滚、索引齐全、保留期裁剪、
在线备份、真实入群天数、emoji 过滤、文案渲染、信息卡渲染。
改完代码先跑这个再传服务器。

---

## 已知待验证（第一次上线请确认）

1. **合并转发帮助**：`Comp.Nodes(nodes=[...])` 的参数名按当前文档写的，
   若日志报错，把 `help_style` 改成 `text` 即可回退，功能都在。
2. **`join_time` 字段**：NapCat 的 `get_group_member_info` 一般会返回。
   若某个版本没有，会自动退化成「插件首次见到他的日期」，不会报错。
   想确认的话看日志或临时打印 `get_group_member_info` 的返回。
3. **禁言时长**：QQ 侧上限 30 天，超出会被自动截断。

---

## 后续可以往上加（已留接口）

- 入群欢迎 / 自动同步群名片：`on_event` 里已有 notice 分支的入口（`_handle_notice`）。
- 骰子、抽卡、签到：都是新的路由分支，额度直接走 `db.add_quota`。
- 图文并发回复：`event.chain_result([Image(...), Plain(...)])`。
- 接 AI：在 `_route` 最后加兜底分支。
