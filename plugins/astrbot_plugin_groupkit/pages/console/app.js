/**
 * 群助手配置台。
 *
 * 与后端的唯一通道是宿主注入的 window.AstrBotPluginPage —— 它负责拼路由、
 * 带鉴权、解 JSON，所以这里不出现任何 fetch，也不硬编码插件名。
 *
 * 这一版的信息架构换过一次，原因值得写下来：
 * 上一版把「群概览」放在第一屏，配置挤在第六个 tab 里的一个小块里，所有
 * 分组一次性铺满 —— 结果是一个**看着像数据看板、用起来像配置页**的四不像。
 * 但这个页面存在的理由只有一个：**配置机器人**。发言统计、成员列表、流水，
 * 全都是「顺便能看」，属于次要模块。
 *
 * 所以现在的层次是：
 *
 *   配置  ← 主体。左边分组目录，右边只显示当前这一组，一屏 3~8 个字段
 *   素材  ← 图片就是配置的一部分（丢一张图 = 上架一件商品）
 *   数据  ← 报表、流水、成员，收在第三个 tab 里，默认不加载
 *
 * 三条硬约束没变：
 * 1. 不新增任何外部依赖（CDN、图表库、图标字体）。插件跑在内网服务器上，
 *    一个外链就是一次白屏。
 * 2. 所有来自数据库的文本一律转义。群名和 QQ 昵称是用户可以随便填的，
 *    直接拼进 innerHTML 等于把 XSS 口子开在管理台上。
 * 3. 空数据必须说清「是还没有数据」还是「没读出来」。后端用 success 字段
 *    区分这两种情况，前端不能都渲染成空白表格。
 */

const bridge = window.AstrBotPluginPage;

const state = {
  view: "config",
  dataSub: "overview",

  config: {},          // 配置页的整页数据（分组 + 字段 + 当前值）
  configLoaded: false,
  configSection: "",   // 当前正在看哪一组
  dirty: new Map(),    // 改过但还没保存的字段：key -> 新值

  assets: null,

  overview: null,
  groups: [],
  groupId: "",
  trend: [],
  rankBy: "streak",
  rank: [],
  members: { rows: [], total: 0, offset: 0, limit: 50, loaded: "" },
  memberQuery: "",
  checkins: null,
  quotas: null,
  storage: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 小工具 */

// 转义后再进 innerHTML。宁可多转一次，也不让昵称里的尖括号跑成标签。
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

const num = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString("zh-CN") : "0";
};

function todayKey() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function shortDate(key) {
  const text = String(key || "");
  if (!text) return "—";
  const today = todayKey();
  if (text === today) return "今天";
  const [year, month, day] = text.split("-");
  if (!month || !day) return text;
  return `${month}-${day}`;
}

function weekday(key) {
  const labels = ["日", "一", "二", "三", "四", "五", "六"];
  const parsed = new Date(`${key}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? "" : labels[parsed.getDay()];
}

let toastTimer = 0;
function toast(message, tone = "normal") {
  const el = $("toast");
  if (!el) return;
  el.textContent = message;
  el.className = `toast is-show${tone === "error" ? " is-error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.className = "toast";
  }, 3000);
}

function showBanner(message) {
  const el = $("banner");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = message;
}

function setHealth(ok, text) {
  const el = $("health");
  if (!el) return;
  // 三态：true = 正常、false = 出问题、null = 没数据但没事。
  const tone = ok === false ? " is-bad" : ok === null ? " is-idle" : "";
  el.className = `health${tone}`;
  $("healthText").textContent = text;
}

/* ------------------------------------------------------------------ 接口 */

async function apiGet(path, params = {}) {
  if (!bridge || typeof bridge.apiGet !== "function") {
    throw new Error("请从 AstrBot 的插件页面进入");
  }
  const result = await bridge.apiGet(path, params);
  if (result && result.success === false) {
    throw new Error(result.error || "请求失败");
  }
  return result || {};
}

async function apiPost(path, body = {}) {
  if (!bridge || typeof bridge.apiPost !== "function") {
    throw new Error("请从 AstrBot 的插件页面进入");
  }
  const result = await bridge.apiPost(path, body);
  if (result && result.success === false) {
    throw new Error(result.error || "请求失败");
  }
  return result || {};
}

/* ================================================================ 配置页
 *
 * 字段不是一个一个写死的，而是后端从 _conf_schema.json 读出来下发的。
 * 所以「页面上有哪些项」等于「配置里有几项」，加了配置项这里自动出现。
 *
 * 一次只渲染一组，而不是全部铺开。这不是为了省代码 —— 47 个字段铺在
 * 一屏里，标签、开关、滑杆、说明全挤在一起，没有视线落点，读不下去。
 * 分组之后每组 3~8 项，一屏正好，右边还能放下这一组的解释。
 *
 * 只提交**改过的**字段，不整页覆盖：整页提交会把别人同时在 WebUI 里
 * 改的值悄悄按旧值写回去，而且「保存了 47 项」这种提示看不出改了什么。
 */

function fieldControl(field) {
  const key = esc(field.key);
  const value = field.value;

  if (field.kind === "bool") {
    const on = value === true || value === "true" || value === 1;
    return `
      <label class="config-switch">
        <input type="checkbox" data-key="${key}" ${on ? "checked" : ""} />
        <span class="config-switch-track" aria-hidden="true"></span>
        <span class="config-switch-text" data-role="switch-text">${on ? "已开启" : "已关闭"}</span>
      </label>`;
  }

  if (field.kind === "select") {
    const options = (field.options || [])
      .map(
        (option) =>
          `<option value="${esc(option.value)}" ${String(option.value) === String(value) ? "selected" : ""}>${esc(option.label)}</option>`
      )
      .join("");
    return `<select class="config-select" data-key="${key}">${options}</select>`;
  }

  if (field.kind === "number") {
    const slider = field.slider;
    const step = field.step ?? 1;
    // 没有滑杆范围就只给一个数字框：硬塞一个 0~100 的滑杆会把
    // 「抽奖下限 1」这种值改成别的意思。
    if (!slider) {
      return `<input class="config-input" type="number" step="${esc(step)}" data-key="${key}" value="${esc(value ?? "")}" />`;
    }
    return `
      <div class="config-range">
        <input type="range" min="${esc(slider.min)}" max="${esc(slider.max)}" step="${esc(step)}" data-key="${key}" data-role="range" value="${esc(value ?? 0)}" />
        <input class="config-input" type="number" min="${esc(slider.min)}" max="${esc(slider.max)}" step="${esc(step)}" data-key="${key}" data-role="number" value="${esc(value ?? 0)}" />
      </div>
      <div class="config-scale"><span>${esc(slider.min)}</span><span>${esc(slider.max)}</span></div>`;
  }

  if (field.kind === "list") {
    const text = Array.isArray(value) ? value.join(", ") : String(value ?? "");
    return `<input class="config-input" type="text" data-key="${key}" value="${esc(text)}" placeholder="留空表示全部" />`;
  }

  if (field.wide) {
    return `<textarea class="config-area" data-key="${key}" spellcheck="false">${esc(value ?? "")}</textarea>`;
  }
  return `<input class="config-input" type="text" data-key="${key}" value="${esc(value ?? "")}" />`;
}

function sections() {
  return Array.isArray(state.config?.sections) ? state.config.sections : [];
}

function activeSection() {
  const list = sections();
  if (!list.length) return null;
  return list.find((section) => section.key === state.configSection) || list[0];
}

/** 左侧分组目录。
 *
 * 每一项带两个信息：本组有几项、本组改了几项。
 * 「改了几项」不能省 —— 分屏之后，A 组改了 2 项、切到 B 组接着改，
 * 如果目录上看不出来，人会以为自己没改过，切回去发现还差着。
 */
function renderConfigNav() {
  const host = $("configNav");
  if (!host) return;
  const list = sections();
  if (!state.configLoaded) {
    host.innerHTML = `<div class="empty">正在读取配置…</div>`;
    return;
  }
  if (!list.length) {
    host.innerHTML = `<div class="empty">读不到配置项<br />刷新页面试试</div>`;
    return;
  }
  const active = activeSection();
  host.innerHTML = list
    .map((section) => {
      const keys = (section.fields || []).map((field) => field.key);
      const changed = keys.filter((key) => state.dirty.has(key)).length;
      const isActive = active && section.key === active.key;
      return `
      <button class="rail-item${isActive ? " is-active" : ""}" data-section="${esc(section.key)}" type="button">
        <span class="rail-item-title">${esc(section.title)}</span>
        <span class="rail-item-meta">
          <span>${(section.fields || []).length} 项</span>
          ${changed ? `<span class="rail-badge">改 ${changed}</span>` : ""}
        </span>
      </button>`;
    })
    .join("");
}

function renderConfig() {
  const host = $("configSections");
  if (!host) return;

  renderConfigNav();

  if (!state.configLoaded) {
    host.innerHTML = `<div class="empty">正在读取配置…</div>`;
    return;
  }
  const section = activeSection();
  if (!section) {
    host.innerHTML = `<div class="empty">读不到配置项。<br />插件可能还没来得及注册管理台接口，刷新页面试试。</div>`;
    return;
  }
  if (!state.configSection) state.configSection = section.key;

  const note = $("configNote");
  if (note) note.textContent = `${sections().length} 组 · ${num(state.config.field_count || 0)} 项`;

  const persist = $("configPersist");
  if (persist) {
    persist.textContent =
      state.config.can_persist === false
        ? "当前环境不能写配置文件，改动只在本次运行有效"
        : "改完点下面的保存，立刻生效";
  }

  const tabNote = $("tabConfigNote");
  if (tabNote) tabNote.textContent = `${sections().length} 组 ${num(state.config.field_count || 0)} 项`;

  renderEffective();

  const fields = section.fields || [];
  const changed = fields.filter((field) => state.dirty.has(field.key)).length;

  host.innerHTML = `
    <section class="config-group">
      <div class="config-head">
        <h2 class="config-title">${esc(section.title)}</h2>
        ${changed ? `<span class="config-badge">本组改了 ${changed} 项</span>` : ""}
      </div>
      <p class="config-note">${esc(section.note || "")}</p>
      <div class="config-fields">
        ${fields
          .map(
            (field) => `
          <div class="config-field${field.wide ? " is-wide" : ""}" data-field="${esc(field.key)}">
            <div class="config-label">
              <span>${esc(field.label)}</span>
              <span class="config-key">${esc(field.key)}</span>
            </div>
            ${fieldControl(field)}
            ${field.hint ? `<p class="config-hint">${esc(field.hint)}</p>` : ""}
          </div>`
          )
          .join("")}
      </div>
    </section>`;

  applyDirtyMarks();
}

/** 顶上那条「当前生效」。
 *
 * 为什么要单独一行：配置层会把值收敛一次（上下限写反、0 表示不限购、
 * 上限封顶），页面上显示的是「存了什么」，这一行显示的是「真的在用多少」。
 * 两个数字不一致的时候，一次就能看出来是哪一项被收敛了。
 */
function renderEffective() {
  const el = $("effectiveLine");
  const effective = state.config?.effective;
  if (!el) return;
  if (!effective) {
    el.hidden = true;
    return;
  }
  const base = effective.checkin_base || [0, 0];
  const draw = effective.draw_range || [0, 0];
  const parts = [
    `额度名「${effective.currency_name}」`,
    `签到 ${base[0]}–${base[1]}`,
    `抽奖 ${draw[0]}–${draw[1]}`,
    `暴击 ${Math.round((Number(effective.draw_bonus_chance) || 0) * 100)}%`,
    effective.verify_enabled
      ? `入群验证 ${effective.verify_timeout_minutes} 分钟`
      : effective.welcome_enabled
        ? "只欢迎不验证"
        : "入群不说话",
    Number(effective.log_retention_days) > 0
      ? `流水留 ${effective.log_retention_days} 天`
      : "流水永久保留",
  ];
  el.textContent = `实际生效：${parts.join(" · ")}`;
  el.hidden = false;
}

/** 从字段对应的控件里读出当前值。 */
function readControl(root) {
  // 数字字段优先读数字框：滑杆的 value 会被浏览器夹到 min/max，
  // 用户填 9999 时读滑杆只能拿到 365，等于偷偷改了他的输入。
  const control =
    root.querySelector('[data-role="number"]') || root.querySelector("[data-key]");
  if (!control) return null;
  const field = findField(control.dataset.key);
  if (!field) return null;
  if (field.kind === "bool") return control.checked;
  if (field.kind === "number") {
    const parsed = Number(control.value);
    // 清空输入框时不要当成 0 —— 那会在用户还没打完时就把值写成 0。
    return control.value === "" || !Number.isFinite(parsed) ? field.value : parsed;
  }
  return control.value;
}

function findField(key) {
  for (const section of sections()) {
    const hit = (section.fields || []).find((field) => field.key === key);
    if (hit) return hit;
  }
  return null;
}

function sameValue(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) {
    const left = Array.isArray(a) ? a.join("\u0001") : String(a ?? "");
    const right = Array.isArray(b) ? b.join("\u0001") : String(b ?? "");
    return left === right;
  }
  if (typeof a === "boolean" || typeof b === "boolean") return Boolean(a) === Boolean(b);
  if (typeof a === "number" || typeof b === "number") return Number(a) === Number(b);
  return String(a ?? "") === String(b ?? "");
}

/** 把一个字段标成「改过」并同步滑杆 / 数字框 / 开关文案。 */
function syncField(root) {
  const key = root.dataset.field;
  const field = findField(key);
  if (!field) return;
  const value = readControl(root);
  if (sameValue(value, field.value)) {
    state.dirty.delete(key);
    root.classList.remove("is-dirty");
  } else {
    state.dirty.set(key, value);
    root.classList.add("is-dirty");
  }
  updateSaveBar();
  // 目录上的「改 N」也要跟着动，但别整块重绘 —— 重绘会把焦点从输入框里踢走。
  refreshNavBadges();
}

function refreshNavBadges() {
  document.querySelectorAll(".rail-item").forEach((button) => {
    const section = sections().find((item) => item.key === button.dataset.section);
    if (!section) return;
    const keys = (section.fields || []).map((field) => field.key);
    const changed = keys.filter((key) => state.dirty.has(key)).length;
    const meta = button.querySelector(".rail-item-meta");
    if (!meta) return;
    const badge = meta.querySelector(".rail-badge");
    if (changed && !badge) {
      const span = document.createElement("span");
      span.className = "rail-badge";
      span.textContent = `改 ${changed}`;
      meta.append(span);
    } else if (changed && badge) {
      badge.textContent = `改 ${changed}`;
    } else if (!changed && badge) {
      badge.remove();
    }
  });
}

function applyDirtyMarks() {
  // 重绘之后要把「改了还没存」的状态补回去，否则一切换分组就丢了标记，
  // 用户会以为改动也没了。
  document.querySelectorAll(".config-field").forEach((root) => {
    root.classList.toggle("is-dirty", state.dirty.has(root.dataset.field));
  });
  updateSaveBar();
}

function updateSaveBar() {
  const bar = $("saveBar");
  const counter = $("saveCount");
  if (!bar) return;
  const count = state.dirty.size;
  bar.hidden = count === 0;
  document.body.classList.toggle("has-savebar", count > 0);
  if (counter) counter.textContent = `改了 ${count} 项，还没保存`;
}

async function loadConfig(force = false) {
  if (state.configLoaded && !force) {
    renderConfig();
    return;
  }
  renderConfig();
  try {
    state.config = await apiGet("config");
    state.configLoaded = true;
    renderConfig();
  } catch (error) {
    state.configLoaded = true;
    state.config = { sections: [] };
    renderConfig();
    toast(`配置读取失败：${error.message}`, "error");
  }
}

async function saveConfig() {
  if (!state.dirty.size) return;
  const button = $("saveBtn");
  if (button) {
    button.disabled = true;
    button.textContent = "保存中";
  }
  const values = Object.fromEntries(state.dirty);
  try {
    const result = await apiPost("config-save", { values });
    state.dirty.clear();
    showBanner("");
    await loadConfig(true);
    // 图片根目录可能刚被改过，素材页的目录就变了。
    if (state.assets) await loadAssets();

    const changed = (result.changed || []).length;
    const notes = result.notes || [];
    const problems = result.problems || [];
    if (problems.length) {
      // 被拒的项要说清楚，否则会出现「点了保存但数字没变」。
      showBanner(`有 ${problems.length} 项没保存：${problems.join("；")}`);
      toast(`保存了 ${changed} 项，${problems.length} 项被拒`, "error");
    } else if (result.persisted === false) {
      showBanner(result.warning || "改动只在本次运行有效");
      toast(`保存了 ${changed} 项（未写盘）`);
    } else if (notes.length) {
      // 「被夹到范围」这类说明要留着看，不能用 toast 一闪而过。
      showBanner(notes.join("；"));
      toast(changed ? `已保存 ${changed} 项，有调整` : "没有需要保存的改动");
    } else {
      toast(changed ? `已保存 ${changed} 项` : "没有需要保存的改动");
    }
  } catch (error) {
    toast(`保存失败：${error.message}`, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "保存";
    }
  }
}

/* ================================================================ 素材页 */

const FOLDER_HINTS = {
  checkin: "发「签到」时按种子抽一张，和文字一起发出去",
  catalog: "发「表情列表」时按文件名顺序发出去",
  cards: "男/女是基础名片（按性别自动发、不上架），其余文件名就是商品名",
};

async function loadAssets() {
  const grid = $("folderGrid");
  if (grid) grid.innerHTML = `<div class="empty">正在读取素材目录…</div>`;
  try {
    state.assets = await apiGet("assets");
    renderAssets();
  } catch (error) {
    state.assets = { folders: [], skins: [], missing: true };
    renderAssets();
    toast(`素材读取失败：${error.message}`, "error");
  }
}

function renderAssets() {
  const data = state.assets || {};
  const grid = $("folderGrid");
  const root = $("assetsRoot");
  const skinNote = $("skinNote");
  const body = $("skinBody");
  const folders = Array.isArray(data.folders) ? data.folders : [];
  const skins = Array.isArray(data.skins) ? data.skins : [];

  if (root) {
    root.textContent = data.root ? `根目录 ${data.root}` : "图片根目录未配置";
  }
  if (skinNote) {
    // 基础名片缺一张，就有一半群友的卡会掉到兜底底色上 —— 这件事必须
    // 在页面上说出来，而不是靠群主自己数 cards/ 里有几个文件。
    const base = Array.isArray(data.base_cards) ? data.base_cards : [];
    const missing = base.filter((item) => !item.ready).map((item) => item.name);
    skinNote.textContent = missing.length
      ? `缺基础名片：${missing.join("、")}`
      : `${skins.length} 款在售 · 基础名片齐全`;
    skinNote.classList.toggle("is-warn", missing.length > 0);
  }
  const tabNote = $("tabAssetsNote");
  if (tabNote) {
    tabNote.textContent = folders.length
      ? folders.map((folder) => `${folder.count} 张`).join(" / ")
      : "未配置";
  }

  if (grid) {
    if (data.missing) {
      grid.innerHTML = `<div class="empty">后端没有拿到图片库对象，可能是插件版本不一致</div>`;
    } else if (!folders.length) {
      grid.innerHTML = `<div class="empty">还没有素材目录</div>`;
    } else {
      grid.innerHTML = folders
        .map((folder) => {
          const files = Array.isArray(folder.files) ? folder.files : [];
          const chips = files.length
            ? files.map((name) => `<span class="chip">${esc(name)}</span>`).join("")
            : `<span class="chip is-empty">还没有图</span>`;
          const more = folder.count > files.length ? `<span class="chip">…还有 ${folder.count - files.length} 张</span>` : "";
          return `
          <article class="folder-card${folder.count ? "" : " is-empty"}">
            <div class="folder-top">
              <span class="folder-name">${esc(folder.key)}/</span>
              <span class="folder-count">${num(folder.count)} 张</span>
            </div>
            <p class="folder-title">${esc(folder.title)}</p>
            <p class="folder-note">${esc(FOLDER_HINTS[folder.key] || folder.note || "")}</p>
            <div class="chips">${chips}${more}</div>
            <p class="folder-path" title="${esc(folder.path)}">${esc(folder.path)}</p>
          </article>`;
        })
        .join("");
    }
  }

  if (body) {
    if (!skins.length) {
      body.innerHTML = `<tr><td colspan="3"><div class="empty">cards/ 里还没有图，商店里就不会出现名片皮肤</div></td></tr>`;
    } else {
      body.innerHTML = skins
        .map(
          (skin) => `
        <tr>
          <td><span class="cell-name">${esc(skin.name)}</span></td>
          <td class="num">${Number(skin.price) > 0 ? `${num(skin.price)} 额度` : "免费"}</td>
          <td class="cell-sub">${esc(skin.filename)}</td>
        </tr>`
        )
        .join("");
    }
  }
}

/* ================================================================ 数据页 */

function renderStats() {
  const data = state.overview;
  const host = $("stats");
  if (!host) return;
  if (!data) {
    host.innerHTML = `<div class="empty">读不到概览数据</div>`;
    return;
  }
  const cells = [
    { k: "群", v: num(data.groups), sub: "有档案的群" },
    {
      k: "成员",
      v: num(data.members),
      // 人均发言比「今日新增」更能说明群活不活跃。
      sub: data.members
        ? `人均发言 ${num(Math.round((Number(data.messages_total) || 0) / data.members))} 条`
        : "还没有人说话",
    },
    { k: "今日发言", v: num(data.messages_today), sub: `累计 ${num(data.messages_total)}` },
    {
      k: "今日签到",
      v: num(data.checkins_today),
      sub: `累计 ${num(data.checkins_total)} 次`,
      accent: true,
    },
    { k: "额度总量", v: num(data.quota_total), sub: "所有群加起来" },
    { k: "今日新增", v: num(data.new_today), sub: "第一次说话的人" },
  ];
  host.innerHTML = cells
    .map(
      (cell) => `
      <div class="stat${cell.accent ? " is-accent" : ""}">
        <span class="stat-k">${esc(cell.k)}</span>
        <span class="stat-v">${esc(cell.v)}</span>
        <span class="stat-sub">${esc(cell.sub)}</span>
      </div>`
    )
    .join("");
}

function renderGroups() {
  const host = $("groupList");
  const note = $("groupNote");
  if (!host) return;
  if (!state.groups.length) {
    host.innerHTML = `<div class="empty">还没有任何群有档案。<br />让群友在群里说一句话就会自动建档。</div>`;
    if (note) note.textContent = "0";
    return;
  }
  if (note) note.textContent = String(state.groups.length);

  host.innerHTML = state.groups
    .map((group) => {
      const active = String(group.group_id) === state.groupId ? " is-active" : "";
      // 群号就是这群的唯一身份 —— 数据库里没有群名（要额外调协议端接口拿，
      // 管理台不该为了显示一个名字去请求 IM）。所以让它当主标题。
      return `
      <button class="group${active}" data-group="${esc(group.group_id)}" type="button">
        <span class="group-id">${esc(group.group_id)}</span>
        <span class="group-meta">
          <span>${num(group.members)} 人</span>
          <span>今日 <em>${num(group.messages_today)}</em> 条</span>
        </span>
      </button>`;
    })
    .join("");
}

function renderTrend() {
  const host = $("chart");
  const note = $("trendNote");
  if (!host) return;

  const rows = state.trend || [];
  if (!rows.length) {
    // 空状态别占着 168px 的画布高度留一片白，收起来只留一行说明。
    host.className = "chart is-empty";
    host.innerHTML = `<div class="empty">这个群还没有发言记录</div>`;
    if (note) note.textContent = "—";
    return;
  }
  host.className = "chart";

  const peak = Math.max(...rows.map((row) => Number(row.messages) || 0), 1);
  const total = rows.reduce((sum, row) => sum + (Number(row.messages) || 0), 0);
  if (note) note.textContent = `7 天共 ${num(total)} 条`;

  const today = todayKey();
  host.innerHTML = rows
    .map((row) => {
      const value = Number(row.messages) || 0;
      // 留出顶部空间，最高的那根才不会把数字顶出画布（数字挂在柱顶上方）。
      const height = Math.max(2, Math.round((value / peak) * 78));
      const isToday = row.date === today;
      return `
      <div class="bar-col${isToday ? " is-today" : ""}" title="${esc(row.date)}：${esc(num(value))} 条发言，${esc(num(row.checkins))} 人签到">
        <div class="bar-track">
          <div class="bar-fill" style="height:${height}%">
            <span class="bar-value">${value ? esc(num(value)) : ""}</span>
          </div>
        </div>
        <span class="bar-day">${esc(isToday ? "今天" : weekday(row.date))}</span>
      </div>`;
    })
    .join("");
}

function renderRank() {
  const host = $("rankList");
  if (!host) return;
  const rows = state.rank || [];
  if (!rows.length) {
    // 「还没有人签到」在额度榜上是句谎话 —— 有人靠发言攒到额度、一次没签过。
    // 发言榜同理：一个从没签到但天天说话的人，在「还没有人签到」里是被藏起来的。
    let empty;
    if (state.rankBy === "quota") {
      empty = "这个群还没有额度记录<br />发言和签到都会攒额度";
    } else if (state.rankBy === "message") {
      empty = "这个群还没有人发言<br />说句话就能上榜";
    } else {
      empty = "这个群还没有人签到<br />发「签到」就能领第一份额度";
    }
    host.innerHTML = `<div class="empty">${empty}</div>`;
    return;
  }
  // 前三名用奖牌，和群里的「额度排行」保持同一套记号 —— 管理台和聊天里
  // 看到的名次长得一样，人才不会怀疑两边说的不是一回事。
  const medals = ["🥇", "🥈", "🥉"];
  const by = state.rankBy;
  host.innerHTML = rows
    .map((row, index) => {
      const top = index < 3 ? " is-top" : "";
      const no = medals[index] || `${index + 1}.`;
      // 四种榜的「主数字」不同源：连签看 streak_days、累计看 total_days、
      // 额度看 quota、发言看 total_messages。副数字换成另外半边 ——
      // 一行里同时能看到「签了多少天」和「攒了多少额度」，比让人来回切着对照好读。
      let main;
      let side;
      if (by === "quota") {
        main = `${num(row.quota)} 额度`;
        side = `连签 ${num(row.streak_days)} 天`;
      } else if (by === "message") {
        main = `${num(row.total_messages)} 条`;
        side = `${num(row.quota)} 额度`;
      } else if (by === "total") {
        main = `${num(row.total_days)} 天`;
        side = `连签 ${num(row.streak_days)} 天`;
      } else {
        main = `${num(row.streak_days)} 天`;
        side = `累计 ${num(row.total_days)} 天`;
      }
      const title = row.title_name
        ? `<span class="rank-title">${esc(row.title_name)}</span>`
        : "";
      return `
      <div class="rank-row${top}">
        <span class="rank-no">${esc(no)}</span>
        <span class="rank-name">${esc(row.name)}${title}</span>
        <span class="rank-main">${esc(main)}</span>
        <span class="rank-side">${esc(side)}</span>
      </div>`;
    })
    .join("");
}

function renderMembers() {
  const body = $("memberBody");
  const countNote = $("memberCount");
  const pagerNote = $("pagerNote");
  if (!body) return;

  const page = state.members;
  const rows = page.rows || [];

  if (countNote) {
    countNote.textContent = `${num(page.total)} 人${state.memberQuery ? "（已筛选）" : ""}`;
  }
  if (pagerNote) {
    const from = page.total ? page.offset + 1 : 0;
    const to = Math.min(page.offset + page.limit, page.total);
    pagerNote.textContent = `第 ${num(from)}–${num(to)} 条，共 ${num(page.total)} 条`;
  }
  if ($("prevPage")) $("prevPage").disabled = page.offset <= 0;
  if ($("nextPage")) $("nextPage").disabled = page.offset + page.limit >= page.total;

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7"><div class="empty">${
      state.memberQuery ? "没有匹配的成员" : "这个群还没有成员档案"
    }</div></td></tr>`;
    return;
  }

  body.innerHTML = rows
    .map((row) => {
      const levelTag =
        row.level >= 4
          ? `<span class="tag is-warn">群主</span>`
          : row.level >= 3
            ? `<span class="tag is-warn">管理</span>`
            : "";
      const title = row.title_name
        ? `<span class="tag is-accent">${esc(row.title_name)}</span>`
        : `<span class="tag">无</span>`;
      const skin = row.card_skin
        ? `<span class="tag">${esc(row.card_skin)}</span>`
        : "";
      const sex = row.sex ? ` · ${esc(row.sex)}` : "";
      return `
      <tr>
        <td>
          <div class="cell-name">${esc(row.name)}</div>
          <span class="cell-sub">${esc(row.user_id)}${sex}</span>
        </td>
        <td class="num">${esc(num(row.quota))}</td>
        <td class="num">${esc(num(row.messages))}</td>
        <td class="num">${esc(num(row.total_days))}<span class="cell-sub">连 ${esc(num(row.streak_days))}</span></td>
        <td>${title}${levelTag}${skin}</td>
        <td class="num">${esc(num(row.draws))}</td>
        <td class="num">
          <button class="mini-btn" data-quota-user="${esc(row.user_id)}" data-quota-name="${esc(row.name)}" data-quota-value="${esc(row.quota)}" type="button">调额度</button>
        </td>
      </tr>`;
    })
    .join("");
}

function renderCheckins() {
  const body = $("checkinBody");
  if (!body) return;
  const rows = state.checkins;
  if (rows === null) {
    body.innerHTML = `<tr><td colspan="6"><div class="empty">正在读取…</div></td></tr>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6"><div class="empty">这个群还没有签到记录</div></td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const doubled = row.doubled ? `<span class="tag is-accent">×2</span>` : "";
      // 同一天可能有很多人签到，「今天」会连着出现十几行。
      const clock = String(row.created_at || "").slice(11, 16);
      return `
      <tr>
        <td><div>${esc(shortDate(row.date))}</div><span class="cell-sub">${esc(clock || "—")}</span></td>
        <td><div class="cell-name">${esc(row.name)}</div><span class="cell-sub">${esc(row.user_id)}</span></td>
        <td class="num"><span class="amount-plus">+${esc(num(row.reward))}</span> ${doubled}</td>
        <td class="num">${esc(num(row.streak))}</td>
        <td class="num">${esc(num(row.total))}</td>
        <td class="cell-sub">${esc(row.note || "—")}</td>
      </tr>`;
    })
    .join("");
}

function renderQuotas() {
  const body = $("quotaBody");
  if (!body) return;
  const rows = state.quotas;
  if (rows === null) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty">正在读取…</div></td></tr>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty">还没有额度变动</div></td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const amount = Number(row.amount) || 0;
      const cls = amount > 0 ? "amount-plus" : amount < 0 ? "amount-minus" : "amount-zero";
      const sign = amount > 0 ? "+" : "";
      // 完整时间戳在流水里没人看年份和秒，截成「10-26 09:42」一屏多放几行。
      const stamp = String(row.created_at || "");
      const shortStamp =
        stamp.length >= 16 ? `${stamp.slice(5, 10)} ${stamp.slice(11, 16)}` : stamp || "—";
      return `
      <tr>
        <td class="cell-sub">${esc(shortStamp)}</td>
        <td><div class="cell-name">${esc(row.name)}</div><span class="cell-sub">${esc(row.user_id)}</span></td>
        <td class="num"><span class="${cls}">${sign}${esc(num(amount))}</span></td>
        <td>${esc(row.reason || "—")}</td>
        <td class="cell-sub">${esc(row.operator_id || "—")}</td>
      </tr>`;
    })
    .join("");
}

function renderStorage() {
  const data = state.storage;
  const sizeEl = $("dbSize");
  const schemaEl = $("dbSchema");
  const rowsEl = $("tableRows");
  if (!data) {
    if (sizeEl) sizeEl.textContent = "—";
    if (rowsEl) rowsEl.innerHTML = `<div class="empty">正在读取…</div>`;
    return;
  }
  if (sizeEl) {
    const kb = Number(data.db_kb) || 0;
    sizeEl.textContent = kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${num(kb)} KB`;
  }
  if (schemaEl) schemaEl.textContent = `v${num(data.schema_version)}`;

  const labels = {
    members: "成员档案",
    daily_stats: "每日发言",
    quota_logs: "额度流水",
    op_logs: "管理审计",
    checkins: "签到状态",
    checkin_records: "签到快照",
    member_titles: "已购称号",
    member_skins: "已购名片",
    shop_purchases: "商店购买",
  };
  if (rowsEl) {
    const entries = Object.entries(data.tables || {});
    rowsEl.innerHTML = entries.length
      ? entries
          .map(
            ([name, count]) => `
        <div class="table-line">
          <span><code>${esc(name)}</code> <span class="cell-sub">${esc(labels[name] || "")}</span></span>
          <b>${esc(num(count))}</b>
        </div>`
          )
          .join("")
      : `<div class="empty">没有可读的表</div>`;
  }
}

/* ================================================================ 加载 */

async function loadOverview() {
  try {
    state.overview = await apiGet("overview");
    renderStats();
  } catch (error) {
    setHealth(false, "概览读取失败");
    showBanner(`概览读取失败：${error.message}`);
  }
}

async function loadGroups() {
  try {
    const data = await apiGet("groups");
    state.groups = Array.isArray(data.groups) ? data.groups : [];
    if (!state.groupId && state.groups.length) {
      state.groupId = String(state.groups[0].group_id);
    }
    if (state.groupId && !state.groups.some((g) => String(g.group_id) === state.groupId)) {
      state.groupId = state.groups.length ? String(state.groups[0].group_id) : "";
    }
    renderGroups();
  } catch (error) {
    showBanner(`群列表读取失败：${error.message}`);
  }
}

/** 保证群列表已经拉过一次。
 *
 * 为什么需要它：成员 / 签到 / 额度三个视图都依赖 `state.groupId`，
 * 而 groupId 只有拉过群列表才有。以前「群列表」是挂在群概览视图里顺带
 * 拉的 —— 于是先从别处点进「成员」，页面上就是一张空表，还查不出原因
 * （因为没有任何报错，只是没数据）。现在这三个视图自己保证前置条件。
 */
let groupsLoading = null;

async function ensureGroups() {
  if (state.groups.length) return;
  if (!groupsLoading) {
    groupsLoading = loadGroups().finally(() => {
      groupsLoading = null;
    });
  }
  await groupsLoading;
}

async function loadGroupDetail() {
  if (!state.groupId) {
    state.trend = [];
    state.rank = [];
    renderTrend();
    renderRank();
    return;
  }
  try {
    const [trend, rank] = await Promise.all([
      apiGet("trend", { group_id: state.groupId, days: 7 }),
      apiGet("ranking", { group_id: state.groupId, by: state.rankBy }),
    ]);
    state.trend = Array.isArray(trend.rows) ? trend.rows : [];
    state.rank = Array.isArray(rank.rows) ? rank.rows : [];
    renderTrend();
    renderRank();
  } catch (error) {
    showBanner(`群详情读取失败：${error.message}`);
  }
}

async function loadMembers(force = false) {
  await ensureGroups();
  const key = `${state.groupId}|${state.memberQuery}`;
  if (!force && state.members.loaded === key) {
    renderMembers();
    return;
  }
  if (!state.groupId) {
    state.members = { rows: [], total: 0, offset: 0, limit: 50, loaded: key };
    renderMembers();
    return;
  }
  try {
    const data = await apiGet("members", {
      group_id: state.groupId,
      q: state.memberQuery,
      offset: state.members.offset,
      limit: state.members.limit,
    });
    state.members = {
      rows: Array.isArray(data.rows) ? data.rows : [],
      total: Number(data.total) || 0,
      offset: Number(data.offset) || 0,
      limit: Number(data.limit) || 50,
      loaded: key,
    };
    renderMembers();
  } catch (error) {
    toast(`成员读取失败：${error.message}`, "error");
  }
}

async function loadCheckins(force = false) {
  await ensureGroups();
  if (!force && state.checkins !== null) return;
  state.checkins = null;
  renderCheckins();
  if (!state.groupId) {
    state.checkins = [];
    renderCheckins();
    return;
  }
  try {
    const data = await apiGet("checkin-feed", { group_id: state.groupId, limit: 40 });
    state.checkins = Array.isArray(data.rows) ? data.rows : [];
    renderCheckins();
  } catch (error) {
    state.checkins = [];
    renderCheckins();
    toast(`签到记录读取失败：${error.message}`, "error");
  }
}

async function loadQuotas(force = false) {
  await ensureGroups();
  if (!force && state.quotas !== null) return;
  state.quotas = null;
  renderQuotas();
  if (!state.groupId) {
    state.quotas = [];
    renderQuotas();
    return;
  }
  try {
    const data = await apiGet("quota-feed", { group_id: state.groupId, limit: 40 });
    state.quotas = Array.isArray(data.rows) ? data.rows : [];
    renderQuotas();
  } catch (error) {
    state.quotas = [];
    renderQuotas();
    toast(`额度流水读取失败：${error.message}`, "error");
  }
}

async function loadStorage() {
  try {
    state.storage = await apiGet("storage");
    renderStorage();
  } catch (error) {
    toast(`数据库读取失败：${error.message}`, "error");
  }
}

/* ================================================================ 切视图 */

const DATA_SUBS = {
  overview: () => Promise.all([loadOverview(), loadGroups()]).then(loadGroupDetail),
  members: () => loadMembers(),
  checkin: () => loadCheckins(),
  quota: () => loadQuotas(),
  storage: () => loadStorage(),
};

function switchView(view) {
  state.view = view;
  document.querySelectorAll("#tabs .tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });

  if (view === "config") loadConfig();
  if (view === "assets") {
    if (state.assets) renderAssets();
    else loadAssets();
  }
  if (view === "data") switchSub(state.dataSub);
}

function switchSub(sub) {
  state.dataSub = sub;
  document.querySelectorAll("#dataTabs .tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.sub === sub);
  });
  document.querySelectorAll(".subview").forEach((section) => {
    section.hidden = section.dataset.sub !== sub;
  });
  const loader = DATA_SUBS[sub];
  if (loader) loader();
}

/* ------------------------------------------------------------------ 调额度 */
let quotaDialog = null;

function buildQuotaDialog() {
  if (quotaDialog) return quotaDialog;
  const dialog = document.createElement("dialog");
  dialog.className = "quota-dialog";
  dialog.innerHTML = `
    <form method="dialog" class="quota-form">
      <p class="kicker">Adjust quota</p>
      <h2 class="quota-title">调整额度</h2>
      <p class="quota-who" id="quotaWho"></p>
      <label class="quota-label" for="quotaInput">变动值（正数发放，负数扣除）</label>
      <input class="search" id="quotaInput" type="number" step="1" value="10" autocomplete="off" />
      <p class="quota-hint">会写进额度流水，原因记为「管理台调整」。</p>
      <div class="quota-actions">
        <button class="mini-btn" value="cancel" type="submit">取消</button>
        <button class="mini-btn is-primary" value="ok" type="submit">确定</button>
      </div>
    </form>`;
  document.body.append(dialog);
  quotaDialog = dialog;
  return dialog;
}

function openQuotaDialog(userId, name, balance) {
  const dialog = buildQuotaDialog();
  $("quotaWho").textContent = `${name}（${userId}）· 当前 ${num(balance)} 额度`;
  const input = $("quotaInput");
  input.value = "10";
  dialog.dataset.userId = userId;
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    // 极老的浏览器没有 dialog；退化成一个数字输入，别让功能直接消失。
    const raw = window.prompt(`给 ${name} 调整多少额度？（正数发放，负数扣除）`, "10");
    if (raw) submitQuota(userId, Number(raw));
  }
}

async function submitQuota(userId, delta) {
  if (!Number.isFinite(delta) || delta === 0) {
    toast("请输入一个非 0 的整数", "error");
    return;
  }
  try {
    const data = await apiPost("member-update", {
      group_id: state.groupId,
      user_id: userId,
      delta,
    });
    toast(`${delta > 0 ? "发放" : "扣除"} ${num(Math.abs(delta))} 额度，余额 ${num(data.balance)}`);
    await Promise.all([loadMembers(true), loadOverview(), loadQuotas(true), loadGroups()]);
  } catch (error) {
    toast(`调整失败：${error.message}`, "error");
  }
}

/* ================================================================ 事件绑定 */
function bindEvents() {
  $("tabs")?.addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) switchView(tab.dataset.view);
  });

  $("dataTabs")?.addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) switchSub(tab.dataset.sub);
  });

  // 左侧分组目录：切组时保留未保存的改动（dirty 不参与重绘），
  // 但要把当前组的滚动位置归零，否则从最后一组切到第一组会停在半空。
  $("configNav")?.addEventListener("click", (event) => {
    const button = event.target.closest(".rail-item");
    if (!button) return;
    const key = button.dataset.section;
    if (!key || key === state.configSection) return;
    state.configSection = key;
    renderConfig();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  $("groupList")?.addEventListener("click", async (event) => {
    const button = event.target.closest(".group");
    if (!button) return;
    const next = button.dataset.group;
    if (!next || next === state.groupId) return;
    state.groupId = next;
    state.members.offset = 0;
    state.members.loaded = "";
    state.checkins = null;
    state.quotas = null;
    renderGroups();
    await loadGroupDetail();
    if (state.dataSub === "members") await loadMembers(true);
    if (state.dataSub === "checkin") await loadCheckins(true);
    if (state.dataSub === "quota") await loadQuotas(true);
  });

  $("refreshBtn")?.addEventListener("click", async () => {
    const button = $("refreshBtn");
    button.disabled = true;
    button.textContent = "读取中";
    showBanner("");
    try {
      if (state.view === "config") await loadConfig(true);
      else if (state.view === "assets") await loadAssets();
      else {
        await Promise.all([loadOverview(), loadGroups()]);
        await loadGroupDetail();
        if (state.dataSub === "members") await loadMembers(true);
        if (state.dataSub === "checkin") await loadCheckins(true);
        if (state.dataSub === "quota") await loadQuotas(true);
        if (state.dataSub === "storage") await loadStorage();
      }
      setHealth(true, "已刷新");
    } catch (error) {
      setHealth(false, "刷新失败");
    } finally {
      button.disabled = false;
      button.textContent = "刷新";
    }
  });

  // 配置页：一个 input/change 监听覆盖全部字段，不用给每个控件单独绑。
  $("configSections")?.addEventListener("input", (event) => {
    const root = event.target.closest?.(".config-field");
    if (!root) return;
    const key = root.dataset.field;
    const field = findField(key);
    // 滑杆和数字框是同一个字段的两个控件，拖滑杆要把数字框同步过去。
    if (field && field.kind === "number" && field.slider) {
      const range = root.querySelector('[data-role="range"]');
      const number = root.querySelector('[data-role="number"]');
      if (range && number) {
        if (event.target === range) number.value = range.value;
        if (event.target === number && number.value !== "") range.value = number.value;
      }
    }
    if (field && field.kind === "bool") {
      const label = root.querySelector('[data-role="switch-text"]');
      if (label) label.textContent = event.target.checked ? "已开启" : "已关闭";
    }
    syncField(root);
  });

  $("saveBtn")?.addEventListener("click", saveConfig);

  $("discardBtn")?.addEventListener("click", async () => {
    state.dirty.clear();
    await loadConfig(true);
    toast("已放弃改动");
  });

  // 排行主数字三选一：连签 / 累计 / 额度。三个都摆在面板标题右边，
  // 亮着的就是当前口径 —— 不做成「点一下切下一个」，那种按钮切两轮之后
  // 就没人记得自己在看哪个榜了。
  $("rankSeg")?.addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-rank]");
    if (!btn || btn.dataset.rank === state.rankBy) return;
    state.rankBy = btn.dataset.rank;
    document.querySelectorAll("#rankSeg [data-rank]").forEach((el) => {
      el.classList.toggle("is-active", el.dataset.rank === state.rankBy);
    });
    await loadGroupDetail();
  });

  let searchTimer = 0;
  $("memberSearch")?.addEventListener("input", (event) => {
    const value = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.memberQuery = value.trim();
      state.members.offset = 0;
      state.members.loaded = "";
      loadMembers(true);
    }, 280);
  });

  $("prevPage")?.addEventListener("click", () => {
    state.members.offset = Math.max(0, state.members.offset - state.members.limit);
    state.members.loaded = "";
    loadMembers(true);
  });

  $("nextPage")?.addEventListener("click", () => {
    state.members.offset += state.members.limit;
    state.members.loaded = "";
    loadMembers(true);
  });

  $("memberBody")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-quota-user]");
    if (!button) return;
    openQuotaDialog(
      button.dataset.quotaUser,
      button.dataset.quotaName,
      button.dataset.quotaValue
    );
  });

  // dialog 提交：只有点「确定」才真的改。
  document.addEventListener("submit", (event) => {
    const form = event.target.closest?.(".quota-form");
    if (!form) return;
    const dialog = form.closest("dialog");
    const submitter = event.submitter;
    if (submitter?.value !== "ok") return;
    const uid = dialog?.dataset.userId;
    const delta = Number($("quotaInput").value);
    if (uid) submitQuota(uid, delta);
  });

  // 关页面 / 刷新前提醒：分屏之后改动散在多个分组里，更容易忘了保存。
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

/* ================================================================ 启动 */
async function boot() {
  bindEvents();

  if (!bridge || typeof bridge.apiGet !== "function") {
    setHealth(false, "不在 AstrBot 内");
    showBanner("这个页面需要在 AstrBot 的插件页面里打开，直接双击 HTML 是读不到数据的。");
    return;
  }

  // 首屏只拉配置：这是这个页面存在的理由，其他都是次要模块。
  await loadConfig(true);
  setHealth(null, "配置已加载");

  // 概览只用来在右上角报一个「几个群」，轻量，顺手拉了。
  loadOverview().then(() => {
    const groups = state.overview?.groups ?? 0;
    setHealth(groups ? true : null, groups ? `${num(groups)} 个群在记录` : "还没有群档案");
  });
}

boot();
