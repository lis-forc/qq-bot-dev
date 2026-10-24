"""功能模块。

一个功能一个文件，统一导出「处理函数 + 命令定义」。

新增功能的标准动作：
1. 在本目录建 `xxx.py`，写 `async def handler(ctx, env)`（可以 yield 多条回复）；
2. 在里面定义这个功能的 Command（口令、权限、帮助文案）；
3. 去 registry.py 的 COMMANDS 里加一行。

不准在这里直接改 main.py —— 入口只负责事件编排。
"""
