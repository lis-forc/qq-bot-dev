"""图片素材：三类图、三个文件夹、一套命名约定。

这个插件现在**基本不自己画图**了 —— 长得好看的图交给会做图的人做。
代码只负责三件事：找到图、挑一张、在需要的时候把字排上去。
于是「图放哪、怎么命名」就成了唯一的接口，必须是死约定，不能靠猜。

    assets/images/
      checkin/   签到运势图。发「签到」时随机抽一张，和文字一起发出去。
      catalog/   表情图鉴。发「表情列表」时按文件名顺序全部发出去。
      cards/     个人名片。信息卡拿它当底图往上排字。

三条命名规则，都是为了「你不用改代码就能加图」：

1. **文件名用数字或文字开头都行，排序按「自然序」**（2 在 10 前面），
   所以 `1.png、2.png、10.png` 和 `01-封面.png、02-玩法.png` 都能顺序发。
2. **名片的名字取自文件名（去掉扩展名）**。想单独定价就写
   `樱花@300.png`，`@` 前面是名字、后面是价格；价格留空就走全局价。
3. **认得出的扩展名才收**：png / jpg / jpeg / webp / gif / bmp。
   桌面上常见的 `.psd`、`.xcf` 放进去不会被误当图片。

`cards/` 里有两个**保留名**，它们不是商品：

    男.png / 女.png     基础名片，免费，按群友的性别自动发

之所以用保留名而不是「另开一个目录」：目录只扫一层，多一层就多一套
扫描逻辑，而「男/女」这两个词在中文里不会被误当成皮肤名 —— 判的是
**整个文件名精确相等**，`男装.png` 照样是一件正常商品。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

# 三个子目录。顺序即「后台管理页里的展示顺序」。
CHECKIN_DIR = "checkin"
CATALOG_DIR = "catalog"
CARDS_DIR = "cards"
SUBDIRS = (CHECKIN_DIR, CATALOG_DIR, CARDS_DIR)

# 保留名：基础名片，按性别给，不上架、不售卖。
BASE_SKINS: dict[str, str] = {"男": "male", "女": "female"}

# 文件名里的价格分隔符。`樱花@300.png` 和 `樱花=300.png` 都认，
# 因为有人觉得 @ 好打、有人觉得 = 好打，两种都没歧义。
_PRICE_SEP = re.compile(r"\s*[@=]\s*(\d+)\s*$")
_NUM_CHUNK = re.compile(r"(\d+)")


def natural_key(path: Path) -> tuple:
    """自然排序键：`2.png` 排在 `10.png` 前面。

    直接用字符串排序会把 10 排到 2 前面，图鉴的顺序就乱了 ——
    而图鉴这东西的顺序是作者精心排的（封面、玩法、细节），不能乱。
    """
    parts = _NUM_CHUNK.split(path.name.lower())
    key: list[object] = []
    for index, part in enumerate(parts):
        if index % 2:
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part))
    return tuple(key)


def parse_filename(path: Path) -> tuple[str, int]:
    """从文件名拆出 (商品名, 价格)。价格 0 表示「没写，用全局价」。"""
    stem = path.stem.strip()
    match = _PRICE_SEP.search(stem)
    if not match:
        return stem, 0
    name = stem[: match.start()].strip()
    try:
        price = max(0, int(match.group(1)))
    except ValueError:
        price = 0
    return (name or stem), price


@dataclass(frozen=True)
class Skin:
    """一张名片皮肤。`price` 已经是「最终该收多少」，不是原始值。"""

    name: str
    price: int
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name


class ImageBank:
    """一个根目录下的三堆图。所有方法都不抛异常，找不到就返回空。

    为什么不把路径写死在代码里：这套东西是给「想换图的人」用的，
    他们不会改 Python。所以路径是一个配置项（`image_root`），
    默认 `assets/images`，填绝对路径就指向服务器上任意一个目录。

    扫描带一层目录缓存：靠目录的 mtime 判断有没有增删文件。
    好处是**运行中丢一张图进去立刻生效**，不用重启机器人；
    坏处是编辑器「覆盖保存」同名单图时 mtime 也会变，于是多扫一次 —— 无所谓。
    """

    def __init__(
        self,
        root: Path | str,
        plugin_dir: Path | str,
        default_skin_price: int = 50,
    ):
        base = Path(root) if str(root or "").strip() else Path("assets/images")
        if not base.is_absolute():
            base = Path(plugin_dir) / base
        self.root = base
        self.plugin_dir = Path(plugin_dir)
        self.default_skin_price = max(0, int(default_skin_price))
        # 目录 mtime → (文件名列表)。目录一改就重扫，运行中丢图立刻生效。
        self._cache: dict[str, tuple[float, tuple[Path, ...]]] = {}

    # ------------------------------------------------------------ 内部
    def folder(self, which: str) -> Path:
        return self.root / which

    def files(self, which: str) -> tuple[Path, ...]:
        """某个目录里的图，按展示顺序。给管理台列文件用，业务代码走具体方法。"""
        return self._scan(which)

    def _scan(self, which: str) -> tuple[Path, ...]:
        folder = self.folder(which)
        try:
            stamp = folder.stat().st_mtime
        except OSError:
            # 目录还不存在：返回空，但记住「不存在」这个状态由 stamp=-1 表示，
            # 免得每次调用都 stat 一次失败。
            self._cache[which] = (-1.0, ())
            return ()
        cached = self._cache.get(which)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            files = tuple(
                sorted(
                    (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
                    key=natural_key,
                )
            )
        except OSError:
            files = ()
        self._cache[which] = (stamp, files)
        return files

    # ------------------------------------------------------------ 签到图
    def checkin_images(self) -> tuple[Path, ...]:
        return self._scan(CHECKIN_DIR)

    def pick_checkin(self, seed: str = "") -> Path | None:
        """按种子挑一张运势图。

        用种子而不是 `random.choice`：同一个人同一天重发「签到」，
        抽到的还是同一张图 —— 否则群友会以为「再签一次能换张好运势」。
        """
        pool = self.checkin_images()
        if not pool:
            return None
        digest = hashlib.sha256(("checkin-image|" + str(seed)).encode("utf-8")).digest()
        return pool[int.from_bytes(digest[:8], "big") % len(pool)]

    # ------------------------------------------------------------ 图鉴
    def catalog_images(self) -> tuple[Path, ...]:
        return self._scan(CATALOG_DIR)

    def pick_catalog(self, page: int = 0) -> list[Path]:
        """图鉴要发的图。page=0 表示全部；有页码就只挑那一张（从 1 数）。

        页码越界夹到最后一页，不是返回空 —— 返回空会让上层掉进「文字清单」
        兜底，于是「表情列表 9」在只有一张图时，群友等来的是一串模板名。
        """
        pool = list(self.catalog_images())
        if not pool:
            return []
        if page <= 0:
            return pool
        return [pool[min(page, len(pool)) - 1]]

    # ------------------------------------------------------------ 名片皮肤
    def skins(self) -> tuple[Skin, ...]:
        """在售的名片皮肤。基础名片（男/女）不算商品，这里排除掉。"""
        result: list[Skin] = []
        for path in self._scan(CARDS_DIR):
            name, explicit = parse_filename(path)
            if name in BASE_SKINS:
                continue
            price = explicit or self.default_skin_price
            result.append(Skin(name=name, price=price, path=path))
        return tuple(result)

    def skin_names(self) -> tuple[str, ...]:
        return tuple(skin.name for skin in self.skins())

    def skin(self, name: str) -> Skin | None:
        """按名字找在售皮肤。精确匹配优先，其次唯一包含匹配（允许「樱」找「樱花」）。"""
        word = str(name or "").strip()
        if not word:
            return None
        pool = self.skins()
        for skin in pool:
            if skin.name == word:
                return skin
        hits = [skin for skin in pool if word in skin.name]
        return hits[0] if len(hits) == 1 else None

    def base_card(self, gender: str = "") -> Path | None:
        """基础名片：按性别给，没有性别就当男。

        这是每个群友都有的免费底图 —— 商店里卖的是「换掉它」的权利，
        不是「有底图」的权利。所以一张都没买的人也该拿到一张好看的卡。
        """
        wanted = "女" if str(gender or "").strip() == "女" else "男"
        for path in self._scan(CARDS_DIR):
            if parse_filename(path)[0] == wanted:
                return path
        return None

    def resolve_card(self, name: str = "", gender: str = "") -> Path | None:
        """信息卡该用哪张底图。

        顺序：本人花钱买的名片 → 按性别的基础名片 → None（调用方画纯色兜底）。
        买的那张图被删了不会报错，会自然退回基础名片 —— 群友只会觉得
        「我的名片变回去了」，而不是「查不了信息卡」。
        """
        found = self.skin(name)
        if found is not None:
            return found.path
        return self.base_card(gender)

    # ------------------------------------------------------------ 诊断
    def summary(self) -> dict[str, int]:
        """给管理台用：每个目录里有多少张图。缺目录报 0，不报错。"""
        return {name: len(self._scan(name)) for name in SUBDIRS}

    def ensure_dirs(self) -> dict[str, Path]:
        """建好三个目录（已经存在就跳过）。方便第一次部署。"""
        made: dict[str, Path] = {}
        for name in SUBDIRS:
            folder = self.folder(name)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                made[name] = folder
            except OSError:
                continue
        return made


def iter_names(paths: Iterable[Path]) -> list[str]:
    """文件名列表，给日志/管理台看。"""
    return [path.name for path in paths]
