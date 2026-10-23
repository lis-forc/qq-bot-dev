# qq-bot-dev

QQ 群机器人的开发仓库。技术栈：NapCat（协议端）+ AstrBot v4（aiocqhttp / OneBot v11）+ 自研插件。

## 目录

```
qq-bot-dev/
├── plugins/            自有插件，每个插件一个目录，进仓库
│   └── astrbot_plugin_groupkit/
├── docs/               开发笔记
│   ├── 00-开发前知识梳理.md
│   └── 01-案例插件拆解.md
├── 待改/               捡回来的插件原件，只读，不进仓库
├── 参考/               长期留用的参考插件（九寰 / sign），只读，不进仓库
├── COMMANDS.md         口令总表 —— 加插件前必看
└── README.md
```

`待改/` 和 `参考/` 被 `.gitignore` 挡住。原因是里面是别人的代码：有各自的
`.git`、各自的授权、各自的 `__pycache__`，全部塞进仓库只会让 `git status`
变脏，且分发给别人时有版权问题。需要留档就留在本地。

---

## 加一个插件的完整流程

### 第 1 步：丢原件

把插件整个目录拖进 `待改/`，**不要解压到 `plugins/` 里**：

```
待改/astrbot_plugin_抽卡/
├── main.py
├── metadata.yaml
└── ...
```

原件我一个字都不改。要动就在 `plugins/` 下另起一份。

### 第 2 步：告诉我两件事

只说"改一下"我改不准。至少要说：

1. **要它什么**——是它的数据源（比如票房 API、热搜接口），还是它的玩法（抽卡、养成），还是它的图片生成？
2. **不要它什么**——它自己的口令、它自己的经济系统、它的文言文文案、它的用户表，哪些归零？

举个具体说法：「要它的抽卡逻辑和卡池，不要它的注册和银两，额度走我们的 `额度`，
口令改成 `抽卡`，输出用我们的 emoji 分段风格」。

### 第 3 步：我出改造版

我会在 `plugins/astrbot_plugin_<新名>/` 建一份，然后：

1. 查 `COMMANDS.md` 划口令，确认不撞车
2. 套我们的骨架（`main.py` 单入口 + `core/` + `utils/`）
3. 接我们的 `members` 表（人、额度、天数），不另起一套账
4. 文案改成我们的风格
5. 写 `_conf_schema.json`、`requirements.txt`
6. 跑 `tools/selftest.py` 自测
7. 更新 `COMMANDS.md`，一起提交

### 第 4 步：上服务器

打包 → 丢进服务器 `AstrBot/data/plugins/` → WebUI 重载插件 → 看日志。
（具体命令见 `plugins/astrbot_plugin_groupkit/README.md`，Docker 和源码部署两种都写了。）

---

## 我接手时会自动过的筛子

| 检查 | 不合就怎么办 |
|---|---|
| `metadata.yaml` 的 `support_platforms` 有没有 `aiocqhttp` | 没有 → 事件层要重写，工作量翻倍，先告诉你 |
| 用 `@filter.command` 还是单入口监 ALL | 前者 → 口令带 `/` 前缀，和"零前缀白话"冲突，改路由 |
| 有没有自己的 SQLite / 用户表 | 有 → 要做映射，别让两套账并存 |
| 有没有 `requirements.txt` | 没有 → 换台机器就 `ModuleNotFoundError` |
| 依赖里有没有 `requests` | 有 → 必须换 `aiohttp`/`httpx`，`requests` 会阻塞事件循环 |
| 数据写在哪个目录 | 写插件目录 → 改到 `data/plugin_data/<插件名>/`，否则更新插件会丢数据 |
| 有没有监听 ALL 消息并每条都回复 | 有 → 加冷却，否则群友随便说话就刷屏 |

---

## 常用命令

```bash
# 看状态
git status

# 提交一次改动
git add -A
git commit -m "groupkit: 信息卡砍到 5 行"

# 打一个能跑的版本（回滚用）
git tag -a v0.3.0 -m "信息卡 + 额度 + 群管"

# 看历史
git log --oneline --graph --all

# 回滚某个文件到上一个提交（改废了救命用）
git checkout -- plugins/xxx/main.py

# 整个仓库回到某个 tag（会丢未提交改动，慎用）
git reset --hard v0.3.0
```

## 打包给服务器

一个插件一个 zip，只打插件目录本身，别带 `tools/` 和 `__pycache__`：

```bash
cd plugins
zip -r astrbot_plugin_groupkit.zip astrbot_plugin_groupkit \
    -x "*/__pycache__/*" -x "*/tools/*"
```

解压后必须是这样一层（`main.py` 直接躺在插件目录根下）：

```
AstrBot/data/plugins/astrbot_plugin_groupkit/main.py
```
