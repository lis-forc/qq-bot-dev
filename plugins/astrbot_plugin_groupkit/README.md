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
- **性别**：先取 QQ 资料（`get_group_member_info` 的 `sex`）；空着或不想用 QQ 上的，
  用 `性别 男` 自己覆盖，覆盖之后不会再被协议端同步盖回去。
- **在册天数**：取**真实入群时间**（见下）。
- **新人礼**：第一次发言自动到账 10 额度，只发一次。

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
| `查` | 个人信息卡（图片） |
| `查 @某人` / `查 123456789` | 看别人的信息卡 |
| `改名 李四` | 改信息卡显示名，消耗 1 额度 |
| `性别 男` | 改信息卡的性别（男 / 女 / 保密） |
| `爱好 打游戏` | 设置爱好 |
| `额度` | 余额 + 抽奖进度 |
| `抽奖` | 消耗一次机会，随机 1~5 额度（10% 暴击 +8），回你「还剩几次」 |
| `今日` | 今日 / 本月 / 累计发言 |
| `额度记录` | 最近 5 笔额度变动（谁加的、什么时候） |
| `表情 摸头 @某人` | 做一张表情包，做成才扣额度 |
| `表情列表` / `搜表情 猫` | 看有哪些模板 / 按关键词找 |
| `表情详情 摸头` | 这个模板要几张图几段字 |
| `帮助` | 菜单（合并转发，点开才展开） |

### 2 级 · 助手

| 口令 | 说明 |
|---|---|
| `禁言 @某人` | 不写时长用配置的默认值（默认 10 分钟） |
| `禁言 @某人 1小时` | 写时长就按写的算，最长 30 天 |
| `解禁 @某人` | |
| `改名 @某人 李四` | 代改他人显示名，不扣对方额度 |
| `性别 @某人 女` | 代改他人性别 |

### 3 级 · 管理

| 口令 | 说明 |
|---|---|
| `踢 @某人` | |
| `踢 @某人 拉黑` | 踢出去并拒绝再次入群申请 |
| `发额度 @某人 50` | |
| `扣额度 @某人 50` | 不会扣成负数 |

### 4 级 · 群主

| 口令 | 说明 |
|---|---|
| `全群禁言` / `全群解禁` | 封 / 解整个群 |
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

- **来**：新人礼（默认 10）；每发言 100 条攒 1 次抽奖机会，`抽奖` 得 1~5（10% 概率暴击 +8）。
- **去**：改名（默认 1）。
- 所有变动写 `quota_logs` 表，可审计；群友自己发 `额度记录` 就能看最近 5 笔。

（上面括号里是配置默认值，以 `_conf_schema.json` 为准，别记成别处的数字。）

额度是为后续高阶功能（图卡、AI 对话、小游戏、骰子）预留的通用货币，
加新的消耗点只要调 `db.add_quota(..., -cost, "原因")`。

---

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled_group_ids` | `[]` | 留空 = 所有群 |
| `currency_name` | `额度` | 货币叫什么 |
| `welcome_quota` | `10` | 新人礼，只发一次 |
| `rename_cost` | `1` | 改名消耗 |
| `draw_per` | `100` | 多少条发言换一次抽奖 |
| `draw_min` / `draw_max` | `1` / `5` | 抽奖区间 |
| `draw_bonus_chance` / `draw_bonus_amount` | `0.1` / `8` | 暴击概率与额度 |
| `notify_draw_ready` | `false` | 攒满时在群里提示（默认关，活跃群会刷屏） |
| `count_command_messages` | `false` | 口令是否计入发言 |
| `default_mute_minutes` | `10` | 禁言不写时长时用多少分钟 |
| `info_triggers` | `查,我的信息,名片` | 信息卡触发词，逗号分隔 |
| `help_style` | `forward` | 帮助用合并转发还是纯文字 |
| `card_bg_path` | `assets/card_bg.png` | 信息卡背景，换成立绘即可 |
| `card_font_path` | `assets/fonts/...ttf` | 字体 |
| `card_show_qq` | `false` | 卡上是否显示 QQ 号 |
| `card_show_avatar` | `true` | 右侧是否显示 QQ 头像（拉不到自动用名字首字占位） |
| `meme_api_base_url` | `http://127.0.0.1:2233` | 表情生成服务地址 |
| `meme_cost` | `1` | 做一张表情扣多少额度，0 = 免费 |
| `meme_timeout` | `30` | 表情生成超时秒数 |
| `meme_max_image_mb` | `10` | 单张素材图大小上限 |
| `meme_auto_avatar` | `true` | 图不够时自动补发送者头像 |
| `meme_auto_texts` | `true` | 没写文字时用模板默认文字 |
| `log_retention_days` | `180` | 流水/日志保留天数，0 = 永久 |
| `stats_retention_days` | `730` | 按天统计保留天数，0 = 永久 |
| `owner_default_level` / `admin_default_level` | `4` / `3` | 群角色映射出的权限 |

---

## 表情包

### 先搞清楚谁在画图

**这个插件不画图。** 画图的是 `meme-generator` —— 一个独立的服务，
插件只负责把「谁的头像 + 什么文字」发过去，把它画好的图转回群里。

这样分工的好处很实在：模板库有几百个模板、每个模板一堆素材图，
全部塞进插件会让插件变成几百 MB；而且模板要更新的时候，
重启一个容器就行，不用动插件代码。

### 起服务

两个版本都能用，插件启动时自动探测，你不需要配「我装的是哪个」。

**Python 版**（模板最全）

```bash
docker run -d \
  --name meme-generator \
  -p 172.17.0.1:2233:2233 \
  --restart always \
  -v /root/memeapi/memes:/data/memes \
  -e MEME_DIRS='["/data/memes"]' \
  meetwq/meme-generator:main
```

**Rust 版**（镜像小一个量级、启动快，自带两百多个模板，不用挂目录）

```bash
docker run -d \
  --name meme-generator \
  -p 172.17.0.1:2233:2233 \
  --restart always \
  meetwq/meme-generator-rs:main
```

两者的接口路径不一样，验证命令也不同：

```bash
curl http://172.17.0.1:2233/memes        # Python 版 → ["petpet", "kiss", ...]
curl http://172.17.0.1:2233/meme/keys    # Rust 版   → 模板 key 列表
```

**拿 Python 版的命令去测 Rust 版会得到 404**，这是路径不同，不是服务坏了。

模板库从这几个仓库拿（Python 版克隆到 `/root/memeapi/memes`）：

- `https://github.com/MemeCrafters/meme-generator-contrib`（官方扩展）
- `https://github.com/anyliew/meme_emoji`（表情包大户）
- `https://github.com/LRZ9712/tudou-meme`

一个模板一个目录，放进去重启容器就生效。

如果 AstrBot 本身跑在容器里，`127.0.0.1` 指的是容器自己，
要么把两个容器放同一个 network 用容器名访问，要么填宿主机内网 IP。

### 怎么用

```
表情 摸头 @小明
表情 亲亲 "你好" @小明
表情 摸头                ← 不 @ 就用你自己的头像
表情 摸头 #circle        ← 模板自定义选项，原样透传给服务端
```

素材来源按「你表达得有多明确」排序，位置也决定模板里的角色：

1. 引用消息里的图
2. 这条消息里带的图
3. 消息里贴的图片链接
4. 被 @ 的人的头像（按 @ 的顺序）
5. 还不够就补你自己的头像

所以「引用一张图 + 表情 摸头」和「表情 摸头 @某人」出来的效果不同 ——
前者用的是那张图，后者用的是那个人。

### 扣费

**做成之后才扣。** 服务连不上、图下载失败、模板参数不对，一分不扣。
查模板列表、看详情、搜索都不扣。

同一个人 4 秒内只能做一张，这是防连点，不是防白嫖。

### 排错

| 现象 | 原因 |
|---|---|
| `连不上表情服务` | 容器没起，或 `meme_api_base_url` 填错。AstrBot 在 Docker 里时别填 `127.0.0.1` |
| `接口对不上` | 装的服务不是 meme-generator / meme-generator-rs，或者端口指到了别的东西 |
| `一个模板都没有` | Python 版没挂载 memes 目录；Rust 版正常自带模板，出现这句说明镜像没打全 |
| `没找到「xxx」` | 模板名不对，先发 `表情列表` 或 `搜表情` |
| 一直超时 | 动图模板很吃 CPU，调大 `meme_timeout`，或给容器多分核 |
| 做出来是空白图 | 模板缺素材文件，去容器日志看是哪个模板 |
| 容器日志里一路 404 | 用 Python 版的路径（`/memes`）测了 Rust 版，正常现象 |

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

裁剪完会顺带跑一次 `PRAGMA optimize`（刷新索引统计，毫秒级）。
这里**刻意不做 `VACUUM`**：它会重写整个库文件、期间独占锁，放在热路径上
会卡住群消息。真要回收空间，是停机维护时手动做的事。

### 备份

`db.backup_to(path)` 是 SQLite 在线备份（不锁库），需要定时归档时直接调它。

---

## 目录结构

```
astrbot_plugin_groupkit/
├── main.py                 # 入口：建依赖 + 事件编排 + 每日维护（不放功能）
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── core/                   # 与功能无关的地基
│   ├── config.py           # 配置强类型封装（全部带兜底）
│   ├── database.py         # SQLite：members / daily_stats / quota_logs / op_logs
│   ├── services.py         # 权限 / 资料 / 统计 / 额度，返回文本不直接发
│   ├── context.py          # PluginContext：把上面这些打包，处理函数只接它
│   ├── command.py          # 命令表的数据结构 + 匹配器工厂
│   └── meme_api.py         # meme-generator 的三个接口（不画图，只发请求）
├── domains/                # 一个功能一个文件
│   ├── registry.py         # ★ 命令清单 + 分发器（加功能就改这里）
│   ├── profile.py          # 信息卡 / 改名 / 爱好
│   ├── quota.py            # 额度 / 抽奖 / 额度记录
│   ├── stats.py            # 今日发言 + 每条消息计数
│   ├── sticker.py          # 表情包：参数语法 / 素材组装 / 扣费 / 小图发送
│   ├── admin.py            # 禁言 / 踢人 / 全群 / 发扣额度 / 授权
│   ├── help.py             # 帮助菜单（从 registry 生成，不手写）
│   └── notice.py           # 入群等通知事件
├── utils/
│   ├── router.py           # compact / strip_at / drop_prefix / parse_duration…
│   ├── event.py            # 读原始报文、@ 目标解析
│   ├── onebot.py           # call_action 封装，失败只记日志不抛
│   ├── media.py            # 取消息图 / 引用图 / QQ 头像，统一下载与限流
│   └── card.py             # 信息卡渲染（含 emoji 过滤）
├── assets/                 # 背景图 + 字体
└── tools/
    ├── make_bg.py          # 生成占位背景
    └── selftest.py         # 不启动 AstrBot 的本地自测
```

数据在 `data/plugin_data/astrbot_plugin_groupkit/groupkit.db`（符合 AstrBot 规范，
插件更新不会覆盖）。信息卡临时图在 `.../cards/`，一小时自动清理。

---

## 加一个新功能

功能都在 `domains/` 里，`main.py` 不用改。以「表情包」为例：

**1. 建 `domains/sticker.py`**

```python
from ..core.command import Command, CommandEnv, HelpEntry, prefix

async def sticker(ctx, env: CommandEnv):
    # env.args 是口令后面的部分，env.event 可以发消息
    yield env.event.plain_result("🖼 表情包生成中…")

COMMANDS = [
    Command(
        key="sticker",                       # 唯一标识，重复会在启动时报错
        matcher=prefix(("表情包", "做表情")),   # 口令：表情包 xxx / 做表情 xxx
        handler=sticker,
        level=1,                             # 需要的权限等级；管理功能填 2~4
        perm_short="表情包",                  # 权限页里的简称
        help=(HelpEntry("表情包 文字", "生成一张表情包", page="base", order=60),),
    ),
]
```

**2. 在 `domains/registry.py` 的 `COMMANDS` 里加一行**

```python
from . import sticker
...
    *admin.COMMANDS,
    *sticker.COMMANDS,     # ← 放在哪个位置 = 匹配优先级，注意别被更宽的口令挡住
]
```

做完这两步，帮助菜单、权限页、权限校验全都自动生效。跑一遍
`python tools/selftest.py`，里面有断言盯着「管理命令有没有声明等级」
「帮助里的用法能不能真的命中命令」。

**要扣额度**：任何功能都走同一句，不要自己 UPDATE 余额表——

```python
ok, balance = ctx.db.add_quota(env.group_id, env.sender_id, -cost, "表情包", "system")
if not ok:
    yield env.event.plain_result(f"额度不够，这个要 {cost}")
    return
```

余额判断和扣减是同一条 SQL，并发下也扣不成负数。

---

## 本地自测

```bash
python tools/selftest.py
```

用桩替换 `astrbot.*`，跑 127 项断言：口令解析、配置兜底、抽奖进度数学、
新人礼只发一次、余额不足拒绝扣款、事务异常整体回滚、索引齐全、保留期裁剪、
在线备份、真实入群天数、emoji 过滤、文案渲染、信息卡渲染、并发扣款、
额度记录排版、踢人拉黑的参数传递、命令表结构自检（key 不重复、管理命令声明等级、
帮助用法能命中命令、普通聊天不被误吞）。
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

- **入群欢迎 / 入群验证**：`domains/notice.py`，`handle_notice` 里已经拿到
  `group_increase` 事件和入群时间，加分支即可。
- **骰子、抽卡、表情包、签到**：在 `domains/` 建文件 + `registry.py` 加一行。
  额度统一走 `ctx.db.add_quota`，余额判断和扣减在同一条 SQL 里，并发扣不成负数。
- **图文并发回复**：`event.chain_result([Image(...), Plain(...)])`，
  `domains/profile.py` 的信息卡已经是这个形式。
- **接 AI**：写一个 `domains/ai.py`，用 `custom()` 匹配器做兜底口令
  （`return None` 表示不接），放在 `COMMANDS` 最后一位即可，前面所有命令
  都不命中时才会轮到它。
- **外部接口类功能**（热搜、票房、运势）：同样建 domain，注意这类要
  **先扣额度再请求**，因为烧的是你的额度；本地生成类（表情包、运势图）
  建议**成功后扣**，做不出来不收钱。
