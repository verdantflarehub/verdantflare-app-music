const { appSDK: sdk, dashboardHost: bridge } = window.$wujie?.props || {};

const localChannelSvg =
  '<svg class="channel-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>';

const DEFAULT_MODELS = [
  {
    id: "local-minimax-music3",
    channel: "本地",
    biz: "音乐生成",
    title: "MiniMax Music 3",
    modelId: "minimax-music-3-omni-local",
    desc: "全曲文生音乐候选生成与指定时间窗局部重绘，适合主歌/副歌完整结构编曲与器乐铺底。",
    specLine: "本地 · 32kHz WAV · 15–240 秒",
    tools: ["music.generate", "music.redraw"],
    teamScope: "文生音乐 (T2M)、局部时间窗重绘 (R2M) · 32kHz WAV · 15–240 秒",
    vendorScope: "sglang-omni (Qwen-7B + DiT/DAV + VAE) · 支持中英文歌词结构控制",
    voices: [],
  },
  {
    id: "local-ace-step-15",
    channel: "本地",
    biz: "双人对唱",
    title: "ACE-Step 1.5",
    modelId: "acestep-v15-base-local",
    desc: "双人对唱分轨编排、统一伴奏主干生成与角色分段干声合成，支持按需唤醒推理。",
    specLine: "本地 · 48kHz WAV · 歌词结构驱动",
    tools: ["duet.plan", "duet.instrumental", "duet.vocal", "duet.preview"],
    teamScope: "双人分唱蓝图规划、统一伴奏主干与分角色干声生成 (DUET) · 48kHz WAV",
    vendorScope: "ACE-Step v1.5 Base · 多段落歌词驱动编曲与局部重绘",
    voices: [],
  },
  {
    id: "local-uvr5-roformer",
    channel: "本地",
    biz: "分轨分离",
    title: "UVR5 RoFormer",
    modelId: "mel-band-roformer-big-beta4",
    desc: "高保真伴奏与人声干声分离、去混响与和声剥离，为 RVC 翻唱与母带重混提供洁净干声。",
    specLine: "本地 · 44.1kHz WAV · 伴奏/人声/去混响",
    tools: ["stems.separate"],
    teamScope: "伴奏/人声分离、混响剥离与和声分离 (STEM) · 44.1kHz WAV",
    vendorScope: "MelBand RoFormer Big Beta4 + Dereverb + Karaoke 级联管线",
    voices: [],
  },
  {
    id: "local-rvc-v2",
    channel: "本地",
    biz: "声线转换",
    title: "RVC v2",
    modelId: "rvc-v2-hubert-rmvpe-40k",
    desc: "角色声线转换与人声音色权重训练，内置已安装角色声线模型库，默认用于主唱干声替换。",
    specLine: "本地 · 40kHz / rmvpe · 已安装 2 个人声音色",
    tools: ["voice.convert", "voice.prepare", "voice.train"],
    teamScope: "角色声线转换、训练集切片清洗与 RVC 权重训练 (RVC) · 40kHz WAV",
    vendorScope: "Retrieval-based Voice Conversion v2 (HuBERT + RMVPE 基频提取)",
    voices: [
      { id: "mengsk-comfort-v2-20260901", spec: "40kHz · rmvpe · 已就绪（生产主推）" },
      { id: "mengsk-demo-v1", spec: "40kHz · rmvpe · 已就绪（历史基线）" },
    ],
  },
  {
    id: "local-whisper-small",
    channel: "本地",
    biz: "歌词对齐",
    title: "Whisper Small",
    modelId: "faster-whisper-small-align",
    desc: "已知歌词文本与人声干声强制时间轴对齐，输出行级 LRC 与字级 JSON 时间戳供 MV 消费。",
    specLine: "本地 · LRC 行级 + JSON 字级时间戳",
    tools: ["lyrics.align"],
    teamScope: "人声干声与净版歌词强制时间轴对齐 (LRC) · 导出 .lrc 与 .json",
    vendorScope: "faster-whisper (Whisper Small) + DTW 强制对齐管线",
    voices: [],
  },
  {
    id: "local-pedalboard-mixer",
    channel: "本地",
    biz: "母带混音",
    title: "Pedalboard Mixer",
    modelId: "pedalboard-ffmpeg-loudnorm",
    desc: "多轨空间混音、人声均衡混响与母带动态压限，统一按 -14 LUFS 响度标准交付成片。",
    specLine: "本地 · 48kHz WAV · -14 LUFS 母带交付",
    tools: ["mix.master"],
    teamScope: "人声+伴奏多轨混音、均衡/混响/压限与 -14 LUFS 响度归一化 (MIX)",
    vendorScope: "Spotify Pedalboard + ffmpeg EBU R128 Loudnorm",
    voices: [],
  },
  {
    id: "higgs-minimax-music3",
    channel: "Higgsfield",
    biz: "音乐生成",
    title: "MiniMax Music 3",
    modelId: "higgsfield-minimax-music-3",
    desc: "云端高并发整曲文生音乐与风格化配乐候选生成，适合多候选快速比选与商业曲风探索。",
    specLine: "Higgsfield · 44.1kHz WAV · 30–240 秒",
    tools: ["music.generate"],
    teamScope: "云端文生音乐候选生成 (T2M) · 44.1kHz WAV · 30–240 秒",
    vendorScope: "Higgsfield Audio Generation API · 支持商业曲风标签与完整歌词段落输入",
    voices: [],
  },
  {
    id: "higgs-suno-v45",
    channel: "Higgsfield",
    biz: "音乐生成",
    title: "Suno v4.5 Studio",
    modelId: "higgsfield-suno-v4-5",
    desc: "流行、摇滚与电子编曲整曲生成，适合快速生成高完成度旋律小样与副歌段落参考。",
    specLine: "Higgsfield · 44.1kHz WAV · 30–240 秒",
    tools: ["music.generate", "music.redraw"],
    teamScope: "整曲小样生成与段落重绘 (T2M / R2M) · 44.1kHz WAV · 30–240 秒",
    vendorScope: "Higgsfield Suno v4.5 Provider · 支持器乐纯享与人声整曲模式",
    voices: [],
  },
];

const MCP_GROUPS = [
  {
    title: "1. 工作流预检与素材导入（2 项）",
    tools: [
      ["workflow.preflight", "下游模型服务在线探测与声线模型可用性预检"],
      ["asset.import", "S3 授权音频素材 SHA-256 校验与项目持久化导入"],
    ],
  },
  {
    title: "2. 独唱生成与局部重绘（2 项）",
    tools: [
      ["music.generate", "MiniMax Music 3 整曲文生音乐生成 [T2M]"],
      ["music.redraw", "指定起止时间窗局部音乐重绘 [R2M]"],
    ],
  },
  {
    title: "3. 双人对唱编排（4 项）",
    tools: [
      ["duet.plan", "双人分唱段落切分与对唱蓝图规划 [DUET]"],
      ["duet.instrumental", "对唱统一伴奏主干生成 [DUET]"],
      ["duet.vocal", "角色女声 / 男声独立人声干声生成 [DUET]"],
      ["duet.preview", "双人干声与伴奏对齐试听合成 [DUET]"],
    ],
  },
  {
    title: "4. 分轨分离与去混响（1 项）",
    tools: [["stems.separate", "UVR5 伴奏/人声分离、去混响与和声剥离 [STEM]"]],
  },
  {
    title: "5. 声线准备、训练与转换（3 项）",
    tools: [
      ["voice.prepare", "干声切片清洗与 RVC 训练集构建 [RVC]"],
      ["voice.train", "RVC v2 角色人声权重训练 [RVC]"],
      ["voice.convert", "使用已安装音色 ID 转换主唱干声 [RVC]"],
    ],
  },
  {
    title: "6. 歌词对齐与母带混音（2 项）",
    tools: [
      ["lyrics.align", "Whisper 已知歌词强制对齐并导出 LRC [LRC]"],
      ["mix.master", "多轨均衡/混响/压限与 -14 LUFS 母带交付 [MIX]"],
    ],
  },
];

const STATUS_LIST = ["全部", "排队中", "处理中", "已完成", "失败", "已取消"];

let tasks = [];
let models = [...DEFAULT_MODELS];
let mcpStatus = null;
let currentStatus = "全部";
let currentViewMode = "gallery";
let expandedModelId = "local-rvc-v2";
let currentDetailTask = null;
let activeStemIdx = 0;
let savedTasksScrollY = 0;
let isPageActive = true;
let pollTimer = null;
const previewCache = new Map();

const $ = (s) => document.querySelector(s);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimeSec(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function formatIsoDisplay(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch {
    return String(iso);
  }
}

function parseLrcLines(lrcText) {
  if (!lrcText || typeof lrcText !== "string") return [];
  const rows = [];
  for (const rawLine of lrcText.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const match = line.match(/^\[(\d{2}):(\d{2}(?:\.\d{1,3})?)\]\s*(.*)$/);
    if (match) {
      const mins = Number(match[1]);
      const secs = Number(match[2]);
      const timeStr = `${match[1]}:${Number(match[2]).toFixed(2).padStart(5, "0")}`;
      const text = match[3] || "（器乐间奏）";
      rows.push({ timeStr, seconds: mins * 60 + secs, text });
    }
  }
  return rows;
}

function normalizeTask(raw) {
  const id = raw.task_id || raw.id || "";
  const artifacts = Array.isArray(raw.artifacts) ? raw.artifacts : [];
  const stems = artifacts.map((a) => ({
    artifact_id: a.artifact_id,
    name: a.filename,
    media_type: a.media_type || "application/octet-stream",
    size: a.size || 0,
    sha256: a.sha256 || "",
    download_path: a.download_path || `/artifacts/${a.artifact_id}/content`,
    spec: `${a.media_type?.includes("audio") ? (raw.spec_label || "WAV") : (a.filename?.split(".").pop()?.toUpperCase() || "FILE")} · ${formatBytes(a.size)}`,
  }));
  const parsedLrc = parseLrcLines(raw.lyrics_lrc);
  const lrc =
    parsedLrc.length > 0
      ? parsedLrc
      : [{ timeStr: "00:00.00", seconds: 0, text: `（${raw.workflow_label || raw.operation || "音频产物"}已持久化就绪，可切换上方分轨直接试听或下载）` }];

  const durSec = Number(raw.duration_seconds) || 0;
  return {
    id,
    title: raw.title || id,
    modelLabel: raw.model || "Music MCP",
    channelLabel: raw.channel_label || "本地",
    status: raw.status_label || (raw.status === "running" ? "处理中" : raw.status === "failed" ? "失败" : "已完成"),
    statusClass: raw.status === "running" ? "running" : raw.status === "failed" ? "failed" : "",
    createdAt: formatIsoDisplay(raw.created_at),
    durationText: durSec > 0 ? `时长 ${formatTimeSec(durSec)}` : `产物 ${artifacts.length} 项`,
    typeCode: raw.type_code || "T2M",
    typeLabel: raw.type_label || raw.workflow_label || raw.operation || "音乐任务",
    specLabel: raw.spec_label || "WAV",
    audioLength: raw.audio_length && raw.audio_length !== "—" ? raw.audio_length : durSec > 0 ? `${Math.round(durSec)}s` : `${artifacts.length} 轨`,
    project: raw.project_id || "default",
    operation: raw.operation || "music.generate",
    voiceModelId: raw.voice_model_id || "",
    prompt: raw.prompt || `[${raw.operation}] Project ${raw.project_id}`,
    stems,
    lrc,
    rawLrc: raw.lyrics_lrc || "",
    durationSeconds: durSec,
  };
}

async function apiRequest(method, path, body = undefined) {
  if (sdk?.app?.request) {
    const req = { method, path };
    if (body !== undefined) req.body = body;
    const res = await sdk.app.request(req);
    if (res && typeof res === "object" && "data" in res && ("status" in res || "ok" in res)) {
      if (res.status && res.status >= 400) {
        throw new Error(res.data?.code || `HTTP_${res.status}`);
      }
      return res.data;
    }
    return res;
  }
  const init = {
    method,
    headers: { Accept: "application/json" },
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(path, init);
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data?.code || data?.error || `HTTP_${resp.status}`);
  }
  return data;
}

function showBanner(msg, isError = false) {
  const el = $("#statusBanner");
  if (!el) return;
  if (!msg) {
    el.hidden = true;
    el.textContent = "";
    el.className = "status-banner";
    return;
  }
  el.hidden = false;
  el.className = `status-banner${isError ? " error" : ""}`;
  el.textContent = msg;
}

function notifyResize() {
  const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, 480);
  try {
    bridge?.resize?.(height);
  } catch {}
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: "resize", height }, "*");
    }
  } catch {}
}

function applyTheme(themeValue) {
  const t = themeValue === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = t;
}

function initThemeAndBridge() {
  try {
    const initial = sdk?.theme?.get?.();
    if (initial) {
      applyTheme(typeof initial === "object" ? initial.resolved || initial.mode : initial);
    } else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
      applyTheme("dark");
    }
    sdk?.theme?.subscribe?.((next) => {
      applyTheme(typeof next === "object" ? next.resolved || next.mode : next);
    });
  } catch {}

  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (d && typeof d === "object") {
      if (d.type === "theme" && d.theme) applyTheme(d.theme);
      if (d.type === "route" && typeof d.route === "string") {
        location.hash = d.route.startsWith("#") ? d.route : `#${d.route}`;
      }
    }
  });

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => notifyResize());
    ro.observe(document.body);
  }

  try {
    sdk?.lifecycle?.onActivated?.(() => {
      isPageActive = true;
      refreshDashboardData(true);
    });
    sdk?.lifecycle?.onDeactivated?.(() => {
      isPageActive = false;
    });
  } catch {}

  document.addEventListener("visibilitychange", () => {
    isPageActive = document.visibilityState !== "hidden";
  });
}

function updateProjectFilterOptions() {
  const select = $("#projectFilter");
  if (!select) return;
  const currentVal = select.value || "all";
  const projects = Array.from(new Set(tasks.map((t) => t.project))).filter(Boolean).sort();
  select.innerHTML = [
    '<option value="all">全部项目</option>',
    ...projects.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`),
  ].join("");
  if (projects.includes(currentVal)) {
    select.value = currentVal;
  } else {
    select.value = "all";
  }
}

function renderTaskSection() {
  const q = ($("#searchInput")?.value || "").trim().toLowerCase();
  const ch = $("#channelFilter")?.value || "all";
  const proj = $("#projectFilter")?.value || "all";

  const base = tasks.filter((t) => {
    const matchQ =
      !q ||
      `${t.title} ${t.prompt} ${t.modelLabel} ${t.project} ${t.voiceModelId} ${t.typeCode}`.toLowerCase().includes(q);
    const matchCh = ch === "all" || t.channelLabel === ch;
    const matchProj = proj === "all" || t.project === proj;
    return matchQ && matchCh && matchProj;
  });

  const filtered = base.filter((t) => currentStatus === "全部" || t.status === currentStatus);

  $("#tabTaskCount").textContent = String(tasks.length);

  $("#statusPills").innerHTML = STATUS_LIST.map((st) => {
    const count = st === "全部" ? base.length : base.filter((x) => x.status === st).length;
    return `<button type="button" class="${currentStatus === st ? "active" : ""}" data-status="${esc(st)}">${esc(st)} (${count})</button>`;
  }).join("");

  if (filtered.length === 0) {
    $("#taskGallery").innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;">当前筛选条件下暂无音乐任务记录</div>`;
    $("#taskTable").innerHTML = `<div class="empty-state">当前筛选条件下暂无音乐任务记录</div>`;
  } else {
    $("#taskGallery").innerHTML = filtered
      .map(
        (t) => `
      <article class="card">
        <div class="thumb">
          <div class="task-identity generation-identity" title="${esc(t.modelLabel)} × ${esc(t.channelLabel)}">
            <strong class="identity-model">${esc(t.modelLabel)}</strong>
            <span class="identity-separator" aria-hidden="true">×</span>
            <strong class="identity-channel">${localChannelSvg}${esc(t.channelLabel)}</strong>
          </div>
          <span class="status-badge ${esc(t.statusClass)}">${esc(t.status)}</span>
          <div class="thumb-wave">
            <svg viewBox="0 0 88 42" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
              <path d="M8 24v-6m8 12V12m8 18V8m8 26V6m8 22V14m8 16V10m8 20v-8m8 12V14m8 10v-6"/>
            </svg>
            <span>点击卡片右下角检视多轨与 LRC</span>
          </div>
          <div class="image-times">
            <span>${esc(t.createdAt)}</span>
            <span>${esc(t.durationText)}</span>
          </div>
        </div>
        <div class="card-body">
          <h2 class="task-title" title="${esc(t.title)}">${esc(t.title)}</h2>
          <p class="prompt">${esc(t.prompt)}</p>
          <div class="card-foot">
            <div class="tags">
              <span class="type-code" title="${esc(t.typeLabel)}">${esc(t.typeCode)}</span>
              <span>${esc(t.specLabel)}</span>
              <span>${esc(t.audioLength)}</span>
            </div>
            <a class="detail-link" href="#task=${encodeURIComponent(t.id)}" data-open-task="${esc(t.id)}">查看详情 →</a>
          </div>
        </div>
      </article>
    `
      )
      .join("");

    $("#taskTable").innerHTML = `
      <table>
        <thead>
          <tr>
            <th>任务名称</th>
            <th>模型 × 渠道</th>
            <th>所属项目</th>
            <th>类型与规格</th>
            <th>创建时间</th>
            <th>状态</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${filtered
            .map(
              (t) => `
            <tr>
              <td><strong>${esc(t.title)}</strong></td>
              <td>${esc(t.modelLabel)} × ${esc(t.channelLabel)}</td>
              <td><code>${esc(t.project)}</code></td>
              <td><div class="tags"><span class="type-code" title="${esc(t.typeLabel)}">${esc(t.typeCode)}</span><span>${esc(t.specLabel)}</span><span>${esc(t.audioLength)}</span></div></td>
              <td>${esc(t.createdAt)}</td>
              <td>${esc(t.status)}</td>
              <td><a class="detail-link" href="#task=${encodeURIComponent(t.id)}" data-open-task="${esc(t.id)}">查看详情 →</a></td>
            </tr>
          `
            )
            .join("")}
        </tbody>
      </table>
    `;
  }

  $("#taskGallery").hidden = currentViewMode !== "gallery";
  $("#taskTable").hidden = currentViewMode !== "table";
  notifyResize();
}

function renderModelCardHtml(m) {
  const isExp = expandedModelId === m.id;
  const channelBadgeIcon =
    m.channel === "本地" ? localChannelSvg : '<span class="higgs-badge" aria-hidden="true">H</span>';

  const voicesHtml =
    m.voices && m.voices.length
      ? `
    <div class="model-inline-kv">
      <span>已安装可用人声音色（voice_model_id）</span>
      <div class="voice-chip-list">
        ${m.voices
          .map(
            (v) => `
          <div class="voice-chip-item">
            <code>${esc(v.id)}</code>
            <span style="color:var(--muted);font-size:11.5px;">${esc(v.spec)}</span>
          </div>
        `
          )
          .join("")}
      </div>
    </div>
  `
      : "";

  const inlineDetailHtml = isExp
    ? `
    <div class="model-inline-detail">
      <div class="model-inline-kv">
        <span>渠道 · 模型标识</span>
        <strong>${esc(m.channel)} · <code>${esc(m.modelId)}</code></strong>
      </div>
      <div class="model-inline-kv">
        <span>关联 MCP 工具</span>
        <strong>${(m.tools || []).map((t) => `<code>${esc(t)}</code>`).join(" / ")}</strong>
      </div>
      <div class="model-inline-kv">
        <span>团队适配范围</span>
        <strong>${esc(m.teamScope)}</strong>
      </div>
      <div class="model-inline-kv">
        <span>供应商声明范围</span>
        <strong>${esc(m.vendorScope)}</strong>
      </div>
      ${voicesHtml}
    </div>
  `
    : "";

  return `
    <article class="model-card ${isExp ? "expanded" : ""}">
      <span class="model-biz-label">${esc(m.biz)}</span>
      <h3 class="model-card-title">${esc(m.title)}</h3>
      <p class="model-desc">${esc(m.desc)}</p>
      <div class="model-channel-pill-row">
        <span class="model-channel-pill">${channelBadgeIcon}${esc(m.channel)}</span>
      </div>
      <div class="model-spec-line">${esc(m.specLine)}</div>
      <button type="button" class="model-expand-trigger" data-toggle-model="${esc(m.id)}">
        <span>${isExp ? "▾" : "▸"}</span>
        <span>查看模型与渠道详情</span>
      </button>
      ${inlineDetailHtml}
    </article>
  `;
}

function renderModelsSection() {
  const viewMode = $("#modelViewModeSelect")?.value || "channel";
  const q = ($("#modelSearchInput")?.value || "").trim().toLowerCase();
  const biz = $("#modelBizSelect")?.value || "all";
  const ch = $("#modelChannelSelect")?.value || "all";

  const filtered = models.filter((m) => {
    const matchQ = !q || `${m.title} ${m.desc} ${m.modelId} ${m.biz}`.toLowerCase().includes(q);
    const matchBiz = biz === "all" || m.biz === biz;
    const matchCh = ch === "all" || m.channel === ch;
    return matchQ && matchBiz && matchCh;
  });

  const channelSet = new Set(filtered.map((m) => m.channel));
  $("#modelSummaryLine").textContent = `${filtered.length} 个模型 · ${channelSet.size} 个渠道`;
  $("#tabModelCount").textContent = String(models.length);

  const container = $("#modelChannelSections");
  if (viewMode === "flat") {
    container.innerHTML = `<div class="model-grid">${filtered.map(renderModelCardHtml).join("")}</div>`;
    notifyResize();
    return;
  }

  const channelOrder = ["本地", "Higgsfield"];
  container.innerHTML = channelOrder
    .map((channelName) => {
      const groupModels = filtered.filter((m) => m.channel === channelName);
      if (!groupModels.length) return "";
      return `
        <section class="channel-group-section">
          <div class="channel-group-head">
            <h2 class="channel-group-title">${esc(channelName)}</h2>
            <span class="channel-group-count">${groupModels.length} 个模型</span>
          </div>
          <div class="model-grid">
            ${groupModels.map(renderModelCardHtml).join("")}
          </div>
        </section>
      `;
    })
    .join("");
  notifyResize();
}

function renderMcpSection() {
  if (mcpStatus) {
    const ver = mcpStatus.app_version || "0.9.5";
    const regOk = mcpStatus.gateway?.registered;
    const statusText = regOk ? "在线 Ready (etcd 已注册)" : "在线 Ready";
    $("#mcpServiceIdentity").innerHTML =
      `verdantflare-music (v${esc(ver)}) · <span style="color:var(--accent);">${esc(statusText)}</span>`;
    const toolCount = Array.isArray(mcpStatus.tools) && mcpStatus.tools.length ? mcpStatus.tools.length : 14;
    $("#tabMcpCount").textContent = String(toolCount);
    $("#mcpContractSummary").textContent = `6 大业务分组 / ${toolCount} 项工具 · :8001 Authority 回读`;
  }

  $("#mcpGroupGrid").innerHTML = MCP_GROUPS.map(
    (g) => `
    <article class="mcp-group-card">
      <strong>${esc(g.title)}</strong>
      <div class="mcp-tool-list">
        ${g.tools
          .map(
            ([name, desc]) => `
          <div class="mcp-tool-item">
            <code>${esc(name)}</code>
            <span style="color:var(--muted);">${esc(desc)}</span>
          </div>
        `
          )
          .join("")}
      </div>
    </article>
  `
  ).join("");
  notifyResize();
}

async function loadAudioPreviewForStem(stem) {
  const audio = $("#audioElement");
  const statusEl = $("#playerStatusText");
  if (!audio || !stem) return;
  audio.pause();
  $("#playToggleBtn").textContent = "▶ 播放";
  $("#waveProgress").style.width = "0%";

  if (!stem.media_type?.startsWith("audio/") && !stem.name?.toLowerCase().endsWith(".wav") && !stem.name?.toLowerCase().endsWith(".mp3")) {
    audio.removeAttribute("src");
    statusEl.textContent = "非音频产物";
    return;
  }

  try {
    statusEl.textContent = "加载试听流...";
    let dataUrl = previewCache.get(stem.artifact_id);
    if (!dataUrl) {
      const res = await apiRequest("GET", `/api/artifacts/${encodeURIComponent(stem.artifact_id)}/preview`);
      if (res?.data_url) {
        dataUrl = res.data_url;
        previewCache.set(stem.artifact_id, dataUrl);
      }
    }
    if (dataUrl) {
      audio.src = dataUrl;
      statusEl.textContent = "🔊 100%";
    }
  } catch (err) {
    statusEl.textContent = "试听流就绪";
  }
}

async function downloadArtifactLossless(artifactId, filename) {
  try {
    showBanner(`正在准备下载 ${filename}...`);
    const first = await apiRequest("GET", `/api/artifacts/${encodeURIComponent(artifactId)}/chunk/0`);
    const totalChunks = Number(first.total_chunks) || 1;
    const chunks = [first.data_b64];
    for (let i = 1; i < totalChunks; i++) {
      const next = await apiRequest("GET", `/api/artifacts/${encodeURIComponent(artifactId)}/chunk/${i}`);
      chunks.push(next.data_b64);
    }
    const byteArrays = chunks.map((b64) => {
      const bin = atob(b64);
      const arr = new Uint8Array(bin.length);
      for (let j = 0; j < bin.length; j++) arr[j] = bin.charCodeAt(j);
      return arr;
    });
    const blob = new Blob(byteArrays, { type: first.media_type || "application/octet-stream" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = first.filename || filename || `${artifactId}.bin`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    showBanner("");
  } catch (err) {
    showBanner(`下载失败：${err.message || err}`, true);
  }
}

function exportTaskLrc(task) {
  if (!task) return;
  const content =
    task.rawLrc ||
    task.lrc.map((row) => `[${row.timeStr}] ${row.text}`).join("\n") + "\n";
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${task.project || task.id}.lrc`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}

async function renderTaskDetail(taskId) {
  let t = tasks.find((x) => x.id === taskId);
  if (!t) {
    try {
      const res = await apiRequest("GET", `/api/tasks/${encodeURIComponent(taskId)}`);
      if (res?.task) t = normalizeTask(res.task);
    } catch {}
  }
  if (!t && tasks.length > 0) t = tasks[0];
  if (!t) return;

  currentDetailTask = t;
  activeStemIdx = 0;
  $("#titleEditBar").hidden = true;
  $("#crumbTaskTitle").textContent = t.title;
  $("#viewTitle").textContent = t.title;
  $("#viewSubtitle").innerHTML = `
    <span style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;">
      <strong>${esc(t.modelLabel)}</strong> × ${localChannelSvg}<strong>${esc(t.channelLabel)}</strong>
      · <span class="tags"><span class="type-code">${esc(t.typeCode)}</span></span> ${esc(t.typeLabel)}
      · ${esc(t.specLabel)} · ${esc(t.audioLength)} · ${esc(t.durationText)}
    </span>
  `;

  const primaryAudioStem =
    t.stems.find((s) => s.media_type?.startsWith("audio/") || s.name?.toLowerCase().endsWith(".wav") || s.name?.toLowerCase().endsWith(".mp3")) ||
    t.stems[0];

  $("#viewActions").innerHTML = `
    <button type="button" class="btn-outline" id="editTitleBtn">编辑标题</button>
    <span class="model-channel-pill">${esc(t.status)}</span>
    ${
      primaryAudioStem
        ? `<button type="button" class="btn-primary" data-download-artifact="${esc(primaryAudioStem.artifact_id)}" data-download-name="${esc(primaryAudioStem.name)}">下载主音频 ↓</button>`
        : ""
    }
    <button type="button" class="btn-outline" id="exportLrcBtn">导出 LRC</button>
  `;

  $("#detailMetaLine").textContent = `创建时间：${t.createdAt} · 所属项目：${t.project}`;
  $("#detailStemTabs").innerHTML = t.stems
    .map(
      (s, idx) => `
    <button type="button" class="stem-tab ${idx === 0 ? "active" : ""}" data-stem-idx="${idx}">
      ${idx === 0 ? "● " : ""}${esc(s.name)}
    </button>
  `
    )
    .join("");

  $("#detailLrcList").innerHTML = t.lrc
    .map(
      (row, idx) => `
    <div class="lrc-row ${idx === 0 ? "active" : ""}" data-lrc-sec="${row.seconds}" data-lrc-time="${esc(row.timeStr)}">
      <code>[${esc(row.timeStr)}]</code>
      <span>${esc(row.text)}</span>
    </div>
  `
    )
    .join("");

  $("#detailPrompt").textContent = t.prompt;
  $("#detailParams").innerHTML = `
    <span style="color:var(--muted);">模型名称</span><strong>${esc(t.modelLabel)}</strong>
    <span style="color:var(--muted);">执行渠道</span><strong>${esc(t.channelLabel)}</strong>
    <span style="color:var(--muted);">任务类型</span><strong>${esc(t.typeCode)} · ${esc(t.typeLabel)}</strong>
    <span style="color:var(--muted);">采样与时长</span><strong>${esc(t.specLabel)} / ${esc(t.audioLength)}</strong>
    ${t.voiceModelId ? `<span style="color:var(--muted);">人声音色 ID</span><strong><code>${esc(t.voiceModelId)}</code></strong>` : ""}
  `;

  $("#detailArtifacts").innerHTML = t.stems
    .map(
      (s, idx) => `
    <div class="mcp-tool-item">
      <span>${idx + 1}. <strong>${esc(s.name)}</strong> <small style="color:var(--muted);">(${esc(s.spec)})</small></span>
      <button type="button" class="btn-outline" data-download-artifact="${esc(s.artifact_id)}" data-download-name="${esc(s.name)}">下载 ↓</button>
    </div>
  `
    )
    .join("");

  const totalFmt = formatTimeSec(t.durationSeconds || 0);
  $("#playerTime").textContent = `00:00 / ${totalFmt}`;
  if (t.stems[0]) {
    loadAudioPreviewForStem(t.stems[0]);
  }
  notifyResize();
}

function syncRoute() {
  const rawHash = location.hash.slice(1) || "tasks";

  if (rawHash.startsWith("task=")) {
    const taskId = decodeURIComponent(rawHash.slice(5));
    $("#unifiedAppHeader").hidden = true;
    $("#detailBreadcrumbBar").hidden = false;
    $("#sectionTasks").hidden = true;
    $("#sectionModels").hidden = true;
    $("#sectionMcp").hidden = true;
    $("#sectionTaskDetail").hidden = false;
    renderTaskDetail(taskId);
    return;
  }

  const audio = $("#audioElement");
  if (audio && !audio.paused) {
    audio.pause();
  }
  $("#titleEditBar").hidden = true;

  $("#unifiedAppHeader").hidden = false;
  $("#detailBreadcrumbBar").hidden = true;
  $("#sectionTaskDetail").hidden = true;

  const tab = ["tasks", "models", "mcp"].includes(rawHash) ? rawHash : "tasks";
  document.querySelectorAll(".app-tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });

  $("#sectionTasks").hidden = tab !== "tasks";
  $("#sectionModels").hidden = tab !== "models";
  $("#sectionMcp").hidden = tab !== "mcp";

  if (tab === "tasks") {
    $("#viewTitle").textContent = "音乐任务";
    $("#viewSubtitle").textContent = "查看、筛选与试听已执行的整曲生成、分轨分离、声线转换与母带混音任务";
    $("#viewActions").innerHTML = `
      <span>已同步 · ${tasks.length} 项持久化任务</span>
      <button type="button" class="btn-outline" id="refreshBtn">刷新</button>
    `;
    renderTaskSection();
    if (savedTasksScrollY > 0) {
      const targetY = savedTasksScrollY;
      savedTasksScrollY = 0;
      requestAnimationFrame(() => window.scrollTo(0, targetY));
    }
  } else if (tab === "models") {
    $("#viewTitle").textContent = "模型目录";
    $("#viewSubtitle").textContent = "按接入渠道（本地 / Higgsfield）分组查看已注册音乐模型、规格边界与可用人声音色";
    $("#viewActions").innerHTML = `
      <span>${models.length} 个模型 · 2 个渠道</span>
      <button type="button" class="btn-outline" id="refreshModelsBtn">刷新目录</button>
    `;
    renderModelsSection();
  } else if (tab === "mcp") {
    $("#viewTitle").textContent = "MCP 服务与能力";
    $("#viewSubtitle").textContent = "查看 Studio 统一 MCP 接入状态、网络隔离边界与各业务分组工具 Schema 定义";
    $("#viewActions").innerHTML = `
      <span>6 大业务分组 · 共 14 项注册工具</span>
    `;
    renderMcpSection();
  }
}

async function refreshDashboardData(silent = false) {
  if (!isPageActive && silent) return;
  try {
    const [dashData, catalogData, mcpData] = await Promise.all([
      apiRequest("GET", "/api/dashboard"),
      apiRequest("GET", "/api/model-catalog").catch(() => null),
      apiRequest("GET", "/api/mcp/status").catch(() => null),
    ]);

    if (dashData?.app_version) {
      $("#appVersionBadge").textContent = `v${dashData.app_version}`;
    }
    if (Array.isArray(dashData?.tasks)) {
      tasks = dashData.tasks.map(normalizeTask);
      updateProjectFilterOptions();
    }
    if (Array.isArray(catalogData?.models) && catalogData.models.length > 0) {
      models = catalogData.models;
    }
    if (mcpData) {
      mcpStatus = mcpData;
    }
    if (!silent) showBanner("");
    syncRoute();
  } catch (err) {
    if (!silent) {
      showBanner(`数据同步失败：${err.message || err}`, true);
    }
    syncRoute();
  }
}

// Interactive Events
document.addEventListener("click", async (e) => {
  const openTaskLink = e.target.closest("[data-open-task]");
  if (openTaskLink) {
    savedTasksScrollY = window.scrollY || document.documentElement.scrollTop || 0;
  }

  const stBtn = e.target.closest("[data-status]");
  if (stBtn) {
    currentStatus = stBtn.dataset.status;
    renderTaskSection();
    return;
  }

  const vmBtn = e.target.closest("[data-view-mode]");
  if (vmBtn) {
    currentViewMode = vmBtn.dataset.viewMode;
    document.querySelectorAll("[data-view-mode]").forEach((b) => b.classList.toggle("active", b === vmBtn));
    renderTaskSection();
    return;
  }

  const modBtn = e.target.closest("[data-toggle-model]");
  if (modBtn) {
    const id = modBtn.dataset.toggleModel;
    expandedModelId = expandedModelId === id ? null : id;
    renderModelsSection();
    return;
  }

  const stemBtn = e.target.closest("[data-stem-idx]");
  if (stemBtn && currentDetailTask) {
    const idx = Number(stemBtn.dataset.stemIdx) || 0;
    activeStemIdx = idx;
    document.querySelectorAll(".stem-tab").forEach((b, i) => {
      const active = i === idx;
      b.classList.toggle("active", active);
      const cleanText = b.textContent.replace(/^●\s*/, "").trim();
      b.textContent = (active ? "● " : "") + cleanText;
    });
    const stem = currentDetailTask.stems[idx];
    if (stem) loadAudioPreviewForStem(stem);
    return;
  }

  const lrcRow = e.target.closest(".lrc-row");
  if (lrcRow) {
    document.querySelectorAll(".lrc-row").forEach((r) => r.classList.toggle("active", r === lrcRow));
    const sec = Number(lrcRow.dataset.lrcSec) || 0;
    const audio = $("#audioElement");
    if (audio && audio.src) {
      audio.currentTime = sec;
      audio.play().catch(() => {});
      $("#playToggleBtn").textContent = "⏸ 暂停";
    } else {
      const total = formatTimeSec(currentDetailTask?.durationSeconds || 0);
      $("#playerTime").textContent = `${formatTimeSec(sec)} / ${total}`;
    }
    return;
  }

  if (e.target.closest("#playToggleBtn")) {
    const audio = $("#audioElement");
    if (!audio) return;
    if (!audio.src && currentDetailTask?.stems?.[activeStemIdx]) {
      await loadAudioPreviewForStem(currentDetailTask.stems[activeStemIdx]);
    }
    if (audio.paused) {
      audio.play().then(() => {
        $("#playToggleBtn").textContent = "⏸ 暂停";
      }).catch(() => {});
    } else {
      audio.pause();
      $("#playToggleBtn").textContent = "▶ 播放";
    }
    return;
  }

  if (e.target.closest("#editTitleBtn") && currentDetailTask) {
    const bar = $("#titleEditBar");
    const input = $("#titleEditInput");
    bar.hidden = false;
    input.value = currentDetailTask.title;
    input.focus();
    notifyResize();
    return;
  }

  if (e.target.closest("#titleCancelBtn")) {
    $("#titleEditBar").hidden = true;
    notifyResize();
    return;
  }

  if (e.target.closest("#titleSaveBtn") && currentDetailTask) {
    const nextTitle = ($("#titleEditInput")?.value || "").trim();
    if (!nextTitle) {
      showBanner("任务标题不能为空", true);
      return;
    }
    try {
      const res = await apiRequest("POST", `/api/tasks/${encodeURIComponent(currentDetailTask.id)}/title`, {
        title: nextTitle,
      });
      if (res?.task) {
        const updated = normalizeTask(res.task);
        currentDetailTask = updated;
        const idx = tasks.findIndex((x) => x.id === updated.id);
        if (idx >= 0) tasks[idx] = updated;
        $("#viewTitle").textContent = updated.title;
        $("#crumbTaskTitle").textContent = updated.title;
        $("#titleEditBar").hidden = true;
        showBanner("");
      }
    } catch (err) {
      showBanner(`保存标题失败：${err.message || err}`, true);
    }
    return;
  }

  const dlBtn = e.target.closest("[data-download-artifact]");
  if (dlBtn) {
    const artId = dlBtn.dataset.downloadArtifact;
    const artName = dlBtn.dataset.downloadName || `${artId}.wav`;
    await downloadArtifactLossless(artId, artName);
    return;
  }

  if (e.target.closest("#exportLrcBtn") && currentDetailTask) {
    exportTaskLrc(currentDetailTask);
    return;
  }

  if (e.target.closest("#refreshBtn") || e.target.closest("#refreshModelsBtn")) {
    await refreshDashboardData(false);
    return;
  }
});

// Audio Player Progress & LRC Sync
const audioEl = $("#audioElement");
if (audioEl) {
  audioEl.addEventListener("timeupdate", () => {
    const cur = audioEl.currentTime || 0;
    const dur = audioEl.duration || currentDetailTask?.durationSeconds || 0;
    $("#playerTime").textContent = `${formatTimeSec(cur)} / ${formatTimeSec(dur)}`;
    if (dur > 0) {
      $("#waveProgress").style.width = `${Math.min(100, (cur / dur) * 100).toFixed(1)}%`;
    }
    const rows = Array.from(document.querySelectorAll(".lrc-row"));
    let activeRow = null;
    for (const r of rows) {
      const s = Number(r.dataset.lrcSec) || 0;
      if (cur >= s) activeRow = r;
    }
    if (activeRow) {
      rows.forEach((r) => r.classList.toggle("active", r === activeRow));
    }
  });
  audioEl.addEventListener("ended", () => {
    $("#playToggleBtn").textContent = "▶ 播放";
  });
}

$("#waveTrack")?.addEventListener("click", (e) => {
  const audio = $("#audioElement");
  if (!audio || !audio.src) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = audio.duration || currentDetailTask?.durationSeconds || 0;
  if (dur > 0) {
    audio.currentTime = ratio * dur;
  }
});

$("#searchInput")?.addEventListener("input", renderTaskSection);
$("#channelFilter")?.addEventListener("change", renderTaskSection);
$("#projectFilter")?.addEventListener("change", renderTaskSection);
$("#modelViewModeSelect")?.addEventListener("change", renderModelsSection);
$("#modelSearchInput")?.addEventListener("input", renderModelsSection);
$("#modelBizSelect")?.addEventListener("change", renderModelsSection);
$("#modelChannelSelect")?.addEventListener("change", renderModelsSection);
window.addEventListener("hashchange", syncRoute);

// Initialize
initThemeAndBridge();
refreshDashboardData(true).finally(() => {
  try {
    bridge?.ready?.();
  } catch {}
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: "ready" }, "*");
    }
  } catch {}
  notifyResize();
});

pollTimer = setInterval(() => {
  if (isPageActive && (!location.hash || location.hash === "#tasks")) {
    refreshDashboardData(true);
  }
}, 10000);
