const { appSDK: sdk, dashboardHost: bridge } = window.$wujie?.props || {};

const localChannelSvg =
  '<svg class="channel-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>';

const sunoChannelSvg =
  '<svg class="channel-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9 15c1.5 1 4.5 1 6 0m-6-6c1.5-1 4.5-1 6 0"/></svg>';

const higgsfieldLogo =
  '<svg width="14" height="14" class="channel-logo" aria-hidden="true" focusable="false" viewBox="0 0 1355 1355" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="1355" height="1355" rx="363" fill="#D1FE17"/><path d="M1088.73 670.726L1087.95 662.156C1080.52 579.906 1026.99 425.16 878.52 425.16C768.341 425.16 685.111 533.914 611.658 629.796C553.046 706.582 502.264 772.452 446.385 772.452C431.533 770.898 412.391 763.49 400.676 746.73C390.125 731.522 387.392 712.035 392.858 688.649C401.448 651.619 450.686 617.315 502.644 580.677C531.161 561.19 560.472 540.529 580.79 520.651C639.402 464.134 669.094 423.205 669.094 357.336C669.094 291.467 632.76 258.716 602.283 244.682C541.331 216.625 451.862 232.994 394.818 282.114C386.227 289.913 377.625 297.31 369.807 304.328C312.371 355.392 273.696 390.086 185 363.571V470.369C302.605 522.216 401.46 423.205 438.97 377.605C467.878 347.592 498.355 330.049 521.024 330.049H522.2C532.359 330.441 540.95 334.34 547.211 341.357C557.37 353.056 561.279 366.699 559.319 381.896C555.018 413.864 521.808 451.273 460.856 492.203C389.352 540.149 269.799 620.444 260.413 721.399C253.379 793.905 290.889 866.4 349.501 894.458C486.247 959.165 569.477 847.684 657.77 729.969C725.365 639.149 789.443 553.01 878.531 553.01C958.625 553.01 988.318 619.271 988.318 660.982V669.173L980.5 670.726C786.318 705.029 680.429 886.67 680.429 970.473C680.429 1054.28 751.541 1126 839.061 1126C941.433 1126 1068.02 1038.69 1088.34 793.123L1089.12 784.162H1170V670.737H1088.73V670.726ZM982.841 796.62C967.216 943.566 891.804 1012.17 846.095 1012.17C825.385 1012.17 796.477 995.021 796.477 963.064C796.477 927.208 850.003 818.454 970.341 786.106L984.409 782.597L982.841 796.632V796.62Z" fill="black"/></svg>';

function channelLogoFor(channel) {
  if (channel === "本地") return localChannelSvg;
  if (channel === "Suno") return sunoChannelSvg;
  return higgsfieldLogo;
}

// 0.7 Gamma 48-bar waveform generator
function buildWaveBars(seed, count = 48, height = 64) {
  let s = 0;
  const str = String(seed ?? "music");
  for (let i = 0; i < str.length; i++) {
    s = (s * 31 + str.charCodeAt(i)) % 9973;
  }
  const bars = [];
  for (let i = 0; i < count; i++) {
    const rawNorm =
      0.18 +
      0.82 *
        Math.abs(
          Math.sin((s + 1) * 0.43 + i * 0.37) * Math.cos((s + 3) * 0.19 + i * 0.13)
        );
    const gammaVal = Math.pow(rawNorm, 0.7);
    const px = Math.max(6, Math.round(gammaVal * height));
    bars.push(`<span class="wave-bar" style="height:${px}px;"></span>`);
  }
  return bars.join("");
}

const DEFAULT_MODELS = [
  {
    id: "local-minimax-music3",
    channel: "本地",
    biz: "音乐生成",
    title: "MiniMax Music 3",
    modelId: "minimax-music-3-omni-local",
    desc: "全曲文生音乐候选生成与指定时间窗局部重绘，适合主歌/副歌完整结构编曲与器乐铺底。",
    specLine: "32kHz WAV · 15–240 秒",
    caps: ["T2M · 文生音乐", "R2M · 局部重绘"],
    refQuota: "无需参考音频 (T2M) / 1 轨源音频 (R2M)",
    maxSize: "≤ 50MB",
    outputSpecs: ["32kHz", "15–240S", "WAV"],
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
    specLine: "48kHz WAV · 歌词结构驱动",
    caps: ["DUET · 双人对唱", "R2M · 段落重绘"],
    refQuota: "伴奏主干 1 轨 + 角色人声干声 2 轨",
    maxSize: "≤ 50MB",
    outputSpecs: ["48kHz", "30–240S", "WAV"],
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
    specLine: "44.1kHz WAV · 伴奏/人声/去混响",
    caps: ["STEM · 分轨分离", "Dereverb · 去混响"],
    refQuota: "输入整曲音频 1 轨（分离伴奏 + 干声）",
    maxSize: "≤ 50MB",
    outputSpecs: ["44.1kHz", "≤ 600S", "WAV"],
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
    specLine: "40kHz / rmvpe · 已安装 2 个人声音色",
    caps: ["RVC · 声线转换", "RMVPE · 基频提取"],
    refQuota: "输入洁净人声干声 1–2 轨",
    maxSize: "≤ 50MB",
    outputSpecs: ["40kHz", "≤ 600S", "WAV"],
    tools: ["voice.convert", "voice.prepare", "voice.train"],
    teamScope: "角色声线转换、训练集切片清洗与 RVC 权重训练 (RVC) · 40kHz WAV",
    vendorScope: "Retrieval-based Voice Conversion v2 (HuBERT + RMVPE 基频提取)",
    voices: [
      { id: "mengsk-comfort-v2-20260901", spec: "40kHz · rmvpe · 生产主推" },
      { id: "mengsk-demo-v1", spec: "40kHz · rmvpe · 历史基线" },
    ],
  },
  {
    id: "local-whisper-small",
    channel: "本地",
    biz: "歌词对齐",
    title: "Whisper Small",
    modelId: "faster-whisper-small-align",
    desc: "已知歌词文本与人声干声强制时间轴对齐，输出行级 LRC 与字级 JSON 时间戳供 MV 消费。",
    specLine: "LRC 行级 + JSON 字级时间戳",
    caps: ["LRC · 强制对齐", "DTW · 字级时间戳"],
    refQuota: "人声干声 1 轨 + 净版歌词文本 1 份",
    maxSize: "≤ 50MB",
    outputSpecs: ["16kHz+", "≤ 600S", "LRC+JSON"],
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
    specLine: "48kHz WAV · -14 LUFS 母带交付",
    caps: ["MIX · 多轨混音", "-14 LUFS · 母带归一"],
    refQuota: "伴奏轨 1 轨 + 角色人声干声 1–2 轨",
    maxSize: "≤ 50MB",
    outputSpecs: ["48kHz", "≤ 600S", "WAV"],
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
    specLine: "44.1kHz WAV · 30–240 秒",
    caps: ["T2M · 云端文生音乐"],
    refQuota: "支持参考曲风音频 0–1 轨",
    maxSize: "≤ 50MB",
    outputSpecs: ["44.1kHz", "30–240S", "WAV"],
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
    specLine: "44.1kHz WAV · 30–240 秒",
    caps: ["T2M · 整曲生成", "R2M · 段落续写/重绘"],
    refQuota: "支持参考音频 0–1 轨 + 封面参考图 0–1 张",
    maxSize: "≤ 50MB",
    outputSpecs: ["44.1kHz", "30–240S", "WAV"],
    tools: ["music.generate", "music.redraw"],
    teamScope: "整曲小样生成与段落重绘 (T2M / R2M) · 44.1kHz WAV · 30–240 秒",
    vendorScope: "Higgsfield Suno v4.5 Provider · 支持器乐纯享与人声整曲模式",
    voices: [],
  },
];

const DEFAULT_MODEL_BY_ID = new Map(DEFAULT_MODELS.map((m) => [m.id, m]));

function enrichModel(raw) {
  const fallback = DEFAULT_MODEL_BY_ID.get(raw.id) || {};
  return {
    ...fallback,
    ...raw,
    caps: Array.isArray(raw.caps) && raw.caps.length ? raw.caps : fallback.caps || [raw.biz || "MUSIC"],
    refQuota: raw.refQuota || fallback.refQuota || "按工具契约传入参考音轨",
    maxSize: raw.maxSize || fallback.maxSize || "≤ 50MB",
    outputSpecs:
      Array.isArray(raw.outputSpecs) && raw.outputSpecs.length === 3
        ? raw.outputSpecs
        : fallback.outputSpecs || ["48kHz", "≤ 240S", "WAV"],
  };
}

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
let models = DEFAULT_MODELS.map(enrichModel);
let mcpStatus = null;
let currentStatus = "全部";
let currentViewMode = "gallery";
let currentModelChannel = "全部";
let currentDetailTask = null;
let activeStemIdx = 0;
let savedTasksScrollY = 0;
let isPageActive = true;
let isComposing = false;
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
  const durSec = Number(raw.duration_seconds) || 0;
  const specRaw = String(raw.spec_label || "48kHz");
  const sampleRate = specRaw.includes("kHz") ? specRaw.split(" ")[0] : "48kHz";
  const channels = raw.channels || "Stereo";
  const format =
    raw.format ||
    (raw.operation === "lyrics.align" ? "LRC+JSON" : "WAV");
  const rawAudioLen =
    raw.audio_length && raw.audio_length !== "—"
      ? String(raw.audio_length).replace(/s$/i, "S")
      : durSec > 0
        ? `${Math.round(durSec)}S`
        : `${artifacts.length}轨`;

  const voiceModelId = raw.voice_model_id || "";
  const refAudios = Array.isArray(raw.ref_audios)
    ? raw.ref_audios
    : stems
        .filter((s) => /stem|vocal|inst|source|input|dry/i.test(s.name))
        .slice(0, 4)
        .map((s) => ({
          name: s.name,
          spec: s.spec,
          artifact_id: s.artifact_id,
          media_type: s.media_type,
        }));
  const voiceConfigs = Array.isArray(raw.voice_configs)
    ? raw.voice_configs
    : voiceModelId
      ? [
          {
            id: voiceModelId,
            role: "主唱目标音色 (RVC v2)",
            params: "40kHz · rmvpe · index_rate=0.66 · protect=0.33",
          },
        ]
      : [];
  const refImages = Array.isArray(raw.ref_images) ? raw.ref_images : [];
  const rawRating =
    typeof raw.content_rating === "string"
      ? raw.content_rating
      : raw.content_rating?.rating || raw.rating || "unrated";

  return {
    id,
    title: raw.title || id,
    contentRating: rawRating,
    content_rating: rawRating,
    modelLabel: raw.model || "Music MCP",
    channelLabel: raw.channel_label || "本地",
    status: raw.status_label || (raw.status === "running" ? "处理中" : raw.status === "failed" ? "失败" : "已完成"),
    statusClass: raw.status === "running" ? "running" : raw.status === "failed" ? "failed" : "",
    createdAt: formatIsoDisplay(raw.created_at),
    durationText: durSec > 0 ? `时长 ${formatTimeSec(durSec)}` : `产物 ${artifacts.length} 项`,
    typeCode: raw.type_code || "T2M",
    typeLabel: raw.type_label || raw.workflow_label || raw.operation || "音乐任务",
    specLabel: raw.spec_label || "WAV",
    sampleRate,
    channels,
    audioLength: rawAudioLen,
    format,
    project: raw.project_id || "default",
    operation: raw.operation || "music.generate",
    voiceModelId,
    prompt: raw.prompt || `[${raw.operation}] Project ${raw.project_id}`,
    stems,
    lrc: parsedLrc,
    rawLrc: raw.lyrics_lrc || "",
    durationSeconds: durSec,
    refAudios,
    voiceConfigs,
    refImages,
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
    el.className = "status-banner backoff-banner";
    return;
  }
  el.hidden = false;
  el.className = `status-banner backoff-banner${isError ? " error" : ""}`;
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
  if (document.body) {
    document.body.dataset.theme = t;
  }
}

function initThemeAndBridge() {
  if (window.$wujie || (window.parent && window.parent !== window)) {
    document.documentElement.dataset.embedded = "true";
  }
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
    const parseSdkPath = (p) => {
      if (typeof p !== "string") return null;
      const pathname = p.split("?")[0];
      if (pathname === "/models") return "models";
      if (pathname === "/mcp") return "mcp";
      if (pathname.startsWith("/tasks/") && pathname.length > 7) return `task=${pathname.slice(7)}`;
      return "tasks";
    };
    const initNav = sdk?.navigation?.get?.();
    if (initNav?.path) {
      const mapped = parseSdkPath(initNav.path);
      if (mapped) currentRoute = mapped;
    }
    sdk?.navigation?.subscribe?.((state) => {
      const mapped = parseSdkPath(state?.path);
      if (mapped && mapped !== currentRoute) syncRoute(mapped, false);
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
      isPageActive = false; resetAudioPreview();
    });
  } catch {}

  document.addEventListener("visibilitychange", () => {
    isPageActive = document.visibilityState !== "hidden";
    if (!isPageActive) resetAudioPreview();
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

function renderTaskActionMenu(t) {
  const isNsfw = t.contentRating === "nsfw" || t.content_rating === "nsfw";
  const nsfwAction = isNsfw ? "clear" : "mark";
  const nsfwLabel = isNsfw ? "移除 18+ 标记" : "标记为 18+ (NSFW)";
  return `
    <div class="card-more-wrap">
      <button type="button" class="card-more-btn" data-menu-toggle="${esc(t.id)}" aria-label="更多操作" aria-haspopup="menu" aria-expanded="false">···</button>
      <div class="card-action-menu" role="menu" data-menu-for="${esc(t.id)}" hidden>
        <button type="button" class="card-action-item" role="menuitem" data-task-action="edit" data-task-id="${esc(t.id)}">编辑</button>
        <button type="button" class="card-action-item" role="menuitem" data-task-action="toggle-nsfw" data-task-id="${esc(t.id)}" data-nsfw-action="${nsfwAction}">${nsfwLabel}</button>
        <button type="button" class="card-action-item danger" role="menuitem" data-task-action="delete" data-task-id="${esc(t.id)}">删除</button>
      </div>
    </div>
  `;
}

function closeAllTaskMenus() {
  document.querySelectorAll(".card-action-menu").forEach((menu) => {
    menu.hidden = true;
  });
  document.querySelectorAll(".card-more-btn[aria-expanded='true']").forEach((btn) => {
    btn.setAttribute("aria-expanded", "false");
  });
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

  if ($("#tabTaskCount")) $("#tabTaskCount").textContent = String(tasks.length);
  if ($("#syncText")) $("#syncText").textContent = `刚刚同步 · 共 ${tasks.length} 项持久化任务`;

  const withDur = filtered.filter((t) => t.durationSeconds > 0);
  if ($("#avgDurationSummary")) {
    if (withDur.length > 0) {
      const avgSec = Math.round(withDur.reduce((acc, t) => acc + t.durationSeconds, 0) / withDur.length);
      const mins = Math.floor(avgSec / 60);
      const secs = String(avgSec % 60).padStart(2, "0");
      $("#avgDurationSummary").textContent = `平均时长 ${mins}m ${secs}s ⓘ`;
    } else {
      $("#avgDurationSummary").textContent = `共 ${filtered.length} 项任务 ⓘ`;
    }
  }

  $("#statusPills").innerHTML = STATUS_LIST.map((st) => {
    const count = st === "全部" ? base.length : base.filter((x) => x.status === st).length;
    return `<button type="button" class="tabular-nums ${currentStatus === st ? "active" : ""}" data-status="${esc(st)}">${esc(st)} (${count})</button>`;
  }).join("");

  if (filtered.length === 0) {
    $("#taskGallery").innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;">当前筛选条件下暂无音乐任务记录</div>`;
    $("#taskTable").innerHTML = `<div class="empty-state">当前筛选条件下暂无音乐任务记录</div>`;
  } else {
    $("#taskGallery").innerHTML = filtered
      .map(
        (t, idx) => `
      <article class="card roving-item" data-open-task="${esc(t.id)}" role="button" tabindex="${idx === 0 ? "0" : "-1"}" aria-label="${esc(t.title)}">
        <div class="thumb">
          <div class="task-identity generation-identity" title="${esc(t.modelLabel)} × ${esc(t.channelLabel)}">
            <strong class="identity-model">${esc(t.modelLabel)}</strong>
            <span class="identity-separator" aria-hidden="true">×</span>
            <strong class="identity-channel">${channelLogoFor(t.channelLabel)}${esc(t.channelLabel)}</strong>
          </div>
          <span class="status-badge ${esc(t.statusClass)}">${esc(t.status)}</span>
          <div class="music-thumb-wave wave-bars" aria-hidden="true">${buildWaveBars(t.id || idx + 1, 48, 64)}</div>
          <div class="image-times tabular-nums">
            <span>${esc(t.createdAt)}</span>
            <span>${esc(t.durationText)}</span>
          </div>
        </div>
        <div class="card-body">
          <h2 class="task-title" title="${esc(t.title)}">${esc(t.title)}</h2>
          <p class="prompt">${esc(t.prompt)}</p>
          <div class="card-foot">
            <div class="tags tabular-nums">
              ${t.contentRating === "nsfw" || t.content_rating === "nsfw" ? '<span class="nsfw-badge">18+</span>' : ""}
              <span class="type-code" title="${esc(t.typeLabel)}">${esc(t.typeCode)}</span>
              <span>${esc(t.sampleRate)}</span>
              <span>${esc(t.channels)}</span>
              <span>${esc(t.audioLength)}</span>
              <span>${esc(t.format)}</span>
            </div>
            ${renderTaskActionMenu(t)}
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
            <th>模式与规格</th>
            <th>创建时间</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${filtered
            .map(
              (t) => `
            <tr data-open-task="${esc(t.id)}" tabindex="0" role="button" aria-label="${esc(t.title)}">
              <td><strong>${esc(t.title)}</strong>${t.contentRating === "nsfw" || t.content_rating === "nsfw" ? ' <span class="nsfw-badge">18+</span>' : ""}</td>
              <td>${esc(t.modelLabel)} × ${esc(t.channelLabel)}</td>
              <td><code>${esc(t.project)}</code></td>
              <td><div class="tags tabular-nums"><span class="type-code" title="${esc(t.typeLabel)}">${esc(t.typeCode)}</span><span>${esc(t.sampleRate)}</span><span>${esc(t.channels)}</span><span>${esc(t.audioLength)}</span><span>${esc(t.format)}</span></div></td>
              <td class="tabular-nums">${esc(t.createdAt)}</td>
              <td>${esc(t.status)}</td>
              <td class="table-actions-cell">${renderTaskActionMenu(t)}</td>
            </tr>
          `
            )
            .join("")}
        </tbody>
      </table>
    `;
  }

  if ($("#taskCountFoot")) {
    $("#taskCountFoot").textContent = `显示 ${filtered.length} / ${tasks.length} 条音乐任务`;
  }

  $("#taskGallery").hidden = currentViewMode !== "gallery";
  $("#taskTable").hidden = currentViewMode !== "table";
  notifyResize();
}

// 零冗余模型服务卡片：按渠道分组后卡片内绝不重复出现渠道名称，也绝不放置“查看”按钮
function renderModelCardHtml(m) {
  const voicesHtml =
    m.voices && m.voices.length
      ? `
    <div style="margin-top:4px;">
      <span>已安装可用人声音色（voice_model_id）：</span>
      <div class="voice-chip-list">
        ${m.voices
          .map(
            (v) => `
          <div class="voice-chip-item">
            <code>${esc(v.id)}</code>
            <span style="color:var(--muted);">${esc(v.spec)}</span>
          </div>
        `
          )
          .join("")}
      </div>
    </div>
  `
      : "";

  return `
    <article class="resource-card">
      <div class="resource-head">
        <div>
          <span class="model-biz-label">${esc(m.biz)}</span>
          <h3 style="margin-top:4px;">${esc(m.title)}</h3>
        </div>
        <code class="model-id-code">${esc(m.modelId)}</code>
      </div>
      <p>${esc(m.desc)}</p>
      <div class="cap-row">
        ${(m.caps || []).map((c) => `<span class="cap-pill"><strong>${esc(c)}</strong></span>`).join("")}
      </div>
      <div class="model-quota-strip">
        <div>参考音频/分轨配额：<strong>${esc(m.refQuota)}</strong></div>
        <div>单次任务最大容量：<strong>${esc(m.maxSize)}</strong> · 关联工具：${(m.tools || []).map((t) => `<code>${esc(t)}</code>`).join(" / ")}</div>
        ${voicesHtml}
      </div>
      <div class="tags tabular-nums" style="margin-top:auto;">
        ${(m.outputSpecs || []).map((s) => `<span>${esc(s)}</span>`).join("")}
      </div>
    </article>
  `;
}

function renderModelsSection() {
  const q = ($("#modelSearchInput")?.value || "").trim().toLowerCase();
  const biz = $("#modelBizSelect")?.value || "all";

  const base = models.filter((m) => {
    const voiceStr = (m.voices || []).map((v) => v.id).join(" ");
    const matchQ = !q || `${m.title} ${m.desc} ${m.modelId} ${m.biz} ${voiceStr}`.toLowerCase().includes(q);
    const matchBiz = biz === "all" || m.biz === biz;
    return matchQ && matchBiz;
  });

  const channelList = ["全部", "本地", "Higgsfield"];
  if ($("#modelChannelTabs")) {
    $("#modelChannelTabs").innerHTML = channelList
      .map((ch) => {
        const count = ch === "全部" ? base.length : base.filter((m) => m.channel === ch).length;
        const label = ch === "全部" ? "全部渠道" : ch;
        return `<button type="button" class="tabular-nums ${currentModelChannel === ch ? "active" : ""}" data-model-channel="${esc(ch)}">${esc(label)} (${count})</button>`;
      })
      .join("");
  }

  const filtered = base.filter((m) => currentModelChannel === "全部" || m.channel === currentModelChannel);
  const channelSet = new Set(filtered.map((m) => m.channel));
  if ($("#modelSummaryLine")) {
    $("#modelSummaryLine").textContent = `当前展示 ${channelSet.size} 个渠道 · ${filtered.length} 个模型`;
  }
  if ($("#tabModelCount")) {
    $("#tabModelCount").textContent = String(models.length);
  }
  if ($("#modelSummaryBadge")) {
    $("#modelSummaryBadge").textContent = `Snapshot · 2 渠道 / ${models.length} 项`;
  }

  const container = $("#modelChannelSections");
  const channelOrder = ["本地", "Higgsfield"];
  container.innerHTML = channelOrder
    .map((channelName) => {
      const groupModels = filtered.filter((m) => m.channel === channelName);
      if (!groupModels.length) return "";
      const channelDesc =
        channelName === "本地"
          ? "集群内网受控声学与音乐推理服务（MiniMax Music 3 / ACE-Step 1.5 / UVR5 / RVC v2 / Whisper / Pedalboard）"
          : "云端高并发整曲生成与商业曲风探索渠道（统一经 Music MCP 适配网关调度）";
      return `
        <section class="channel-group">
          <header class="channel-group-header">
            <div class="channel-title-wrap">
              ${channelLogoFor(channelName)}
              <h3>${esc(channelName)}</h3>
              <span class="quota-badge tabular-nums">${groupModels.length} 个模型</span>
            </div>
            <span class="channel-meta">${esc(channelDesc)}</span>
          </header>
          <div class="resource-grid">
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
    const ver = mcpStatus.app_version || "0.9.16";
    const regOk = mcpStatus.gateway?.registered;
    const statusText = regOk ? "● 在线 Ready (etcd 已注册)" : "● 在线 Ready";
    if ($("#mcpServiceIdentity")) {
      $("#mcpServiceIdentity").innerHTML =
        `verdantflare-music (v${esc(ver)}) · <span style="color:var(--accent);">${esc(statusText)}</span>`;
    }
    const toolCount = Array.isArray(mcpStatus.tools) && mcpStatus.tools.length ? mcpStatus.tools.length : 14;
    if ($("#tabMcpCount")) $("#tabMcpCount").textContent = String(toolCount);
    if ($("#mcpContractSummary")) {
      $("#mcpContractSummary").textContent = `6 大业务分组 · 共 ${toolCount} 项工具 · :8001 Authority 授权回读`;
    }
  }

  $("#mcpGroupGrid").innerHTML = MCP_GROUPS.map(
    (g) => `
    <article class="resource-card">
      <div class="resource-head">
        <h3>${esc(g.title)}</h3>
        <span class="quota-badge">MCP Group</span>
      </div>
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

let audioRevision = 0, audioLoad = null;
function resetAudioPreview() {
  audioRevision++; audioLoad = null;
  const audio = $("#audioElement");
  audio?.pause(); audio?.removeAttribute("src");
  $("#playToggleBtn").textContent = "▶ 播放";
  $("#waveProgress").style.width = "0%";
  $("#playerTime").textContent = "00:00 / --:--";
  document.querySelectorAll("#detailWaveBars .wave-bar").forEach(b => b.classList.remove("played"));
}
function selectAudioStem(stem) {
  resetAudioPreview();
  const available = !!stem && (stem.media_type?.startsWith("audio/") || /\.(wav|mp3|flac|ogg|m4a)$/i.test(stem.name || ""));
  $("#playToggleBtn").disabled = !available;
  $("#detailWaveBars").disabled = !available;
  $("#playerStatusText").textContent = available ? "点击播放" : "暂无可试听音频";
  const download = $("#stageDownloadBtn"); download.disabled = !stem;
  if (stem) { download.dataset.downloadArtifact = stem.artifact_id; download.dataset.downloadName = stem.name; download.textContent = "下载当前音轨 ↓"; }
  else { delete download.dataset.downloadArtifact; delete download.dataset.downloadName; }
}
async function loadAudioPreviewForStem(stem) {
  if (audioLoad) return audioLoad;
  const audio = $("#audioElement"), epoch = audioRevision;
  if (!audio || !stem) return false;
  const work = (async () => {
    try {
      $("#playerStatusText").textContent = "正在加载音频…";
      let dataUrl = previewCache.get(stem.artifact_id);
      if (!dataUrl) {
        const res = await apiRequest("GET", `/api/artifacts/${encodeURIComponent(stem.artifact_id)}/preview`);
        if (!res?.data_url) throw Error("未返回可播放音频");
        dataUrl = res.data_url; previewCache.set(stem.artifact_id, dataUrl);
      }
      if (epoch !== audioRevision || !isPageActive) return false;
      audio.src = dataUrl; $("#playerStatusText").textContent = "点击播放";
      return true;
    } catch (err) {
      if (epoch === audioRevision) $("#playerStatusText").textContent = `加载失败：${err.message || err}，点击重试`;
      return false;
    }
  })();
  audioLoad = work;
  try { return await work; } finally { if (audioLoad === work) audioLoad = null; }
}
let playbackPending = false;
async function toggleAudioPlayback() {
  if (playbackPending) return;
  const audio = $("#audioElement"), stem = currentDetailTask?.stems?.[activeStemIdx], epoch = audioRevision;
  if (!audio || !stem || $("#playToggleBtn").disabled) return;
  playbackPending = true;
  try {
    if (!audio.src && !await loadAudioPreviewForStem(stem)) return;
    if (epoch !== audioRevision || !isPageActive) return;
    if (audio.paused) { await audio.play(); if (epoch === audioRevision) { $("#playToggleBtn").textContent = "⏸ 暂停"; $("#playerStatusText").textContent = "正在播放"; } }
    else { audio.pause(); $("#playToggleBtn").textContent = "▶ 播放"; $("#playerStatusText").textContent = "已暂停"; }
  } catch (err) { if (epoch === audioRevision) $("#playerStatusText").textContent = `播放失败：${err.message || err}，点击重试`; }
  finally { playbackPending = false; }
}

async function hydrateRefAudioPreviews() {
  const audioEls = Array.from(document.querySelectorAll("audio[data-ref-audio-id]"));
  for (const el of audioEls) {
    const artId = el.dataset.refAudioId;
    if (!artId) continue;
    try {
      let dataUrl = previewCache.get(artId);
      if (!dataUrl) {
        const res = await apiRequest("GET", `/api/artifacts/${encodeURIComponent(artId)}/preview`);
        if (res?.data_url) {
          dataUrl = res.data_url;
          previewCache.set(artId, dataUrl);
        }
      }
      if (dataUrl) {
        el.src = dataUrl;
      }
    } catch {}
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

function renderPromptWithChips(t) {
  let html = esc(t.prompt || "");
  html = html.replace(/&lt;Audio\s*([1-4])&gt;/gi, (_, idxStr) => {
    const idx = Math.max(0, Number(idxStr) - 1);
    return `<button type="button" class="prompt-ref-chip" data-focus-ref="ref-aud-${idx}">&lt;Audio ${idx + 1}&gt;</button>`;
  });
  html = html.replace(/&lt;Voice\s*1&gt;/gi, () => {
    return `<button type="button" class="prompt-ref-chip" data-focus-ref="ref-voice-0">&lt;Voice 1&gt;</button>`;
  });
  html = html.replace(/&lt;LRC\s*1&gt;/gi, () => {
    return `<button type="button" class="prompt-ref-chip" data-focus-ref="ref-lrc-box">&lt;LRC 1&gt;</button>`;
  });

  const extraChips = [];
  if (!/&lt;Audio\s*[1-4]&gt;/i.test(esc(t.prompt || "")) && t.refAudios.length > 0) {
    t.refAudios.slice(0, 4).forEach((a, idx) => {
      extraChips.push(
        `<button type="button" class="prompt-ref-chip" data-focus-ref="ref-aud-${idx}" title="${esc(a.name)}">&lt;Audio ${idx + 1}&gt;</button>`
      );
    });
  }
  if (!/&lt;Voice\s*1&gt;/i.test(esc(t.prompt || "")) && t.voiceConfigs.length > 0) {
    extraChips.push(
      `<button type="button" class="prompt-ref-chip" data-focus-ref="ref-voice-0" title="${esc(t.voiceConfigs[0].id)}">&lt;Voice 1&gt;</button>`
    );
  }
  if (!/&lt;LRC\s*1&gt;/i.test(esc(t.prompt || "")) && t.lrc.length > 0) {
    extraChips.push(
      `<button type="button" class="prompt-ref-chip" data-focus-ref="ref-lrc-box" title="定位结构化歌词时间轴">&lt;LRC 1&gt;</button>`
    );
  }
  if (extraChips.length > 0) {
    html += ` <span style="margin-left:4px;">${extraChips.join(" ")}</span>`;
  }
  return html;
}

async function renderTaskDetail(taskId) {
  let t = tasks.find((x) => x.id === taskId);
  if (!t) {
    try {
      const res = await apiRequest("GET", `/api/tasks/${encodeURIComponent(taskId)}`);
      if (res?.task) t = normalizeTask(res.task);
    } catch {}
  }
  if (!t) { showBanner("任务不存在或无权访问", true); return; }

  currentDetailTask = t;
  activeStemIdx = 0;
  if ($("#titleEditBar")) $("#titleEditBar").hidden = true;
  if ($("#crumbCurrentView")) {
    $("#crumbCurrentView").innerHTML = `<a href="#tasks" data-nav-back="tasks">任务</a> <span aria-hidden="true">/</span> <span aria-current="page">${esc(t.title)}</span>`;
  }

  const primaryAudioStem =
    t.stems.find((s) => s.media_type?.startsWith("audio/") || s.name?.toLowerCase().endsWith(".wav") || s.name?.toLowerCase().endsWith(".mp3")) ||
    t.stems[0];

  // .inspect-head
  if ($("#detailShellTitle")) {
    $("#detailShellTitle").textContent = t.title;
  }
  const totalBytes = t.stems.reduce((acc, s) => acc + (Number(s.size) || 0), 0);
  if ($("#detailHeadBadges")) {
    $("#detailHeadBadges").innerHTML = `
      <span class="identity-channel" id="detailHeadModelChannel" style="font-size:12px;color:var(--accent);font-weight:600">${esc(t.modelLabel)} × ${channelLogoFor(t.channelLabel)}${esc(t.channelLabel)}</span>
      <span class="cap-pill"><strong>${esc(t.typeCode)}</strong><small>${esc(t.typeLabel)}</small></span>
      <span class="quota-badge tabular-nums">单任务参考容量：${formatBytes(totalBytes)} / ≤ 50 MB</span>
    `;
  }
  if ($("#detailStatus")) $("#detailStatus").textContent = t.status;
  if ($("#detailMetaLine")) {
    $("#detailMetaLine").textContent = `规格：${t.sampleRate} / ${t.audioLength} / ${t.channels} / ${t.format} · 创建时间：${t.createdAt} · 所属项目：${t.project}`;
  }

  // Left .output-pane
  if ($("#detailStemCountBadge")) {
    $("#detailStemCountBadge").textContent = `${t.stems.length} 轨音轨就绪`;
  }
  const activeStem = t.stems[0] || primaryAudioStem || { name: "Final Master 母带", spec: `${t.sampleRate} · ${t.format}` };
  if ($("#activeStemTitle")) {
    $("#activeStemTitle").textContent = activeStem.name;
  }
  if ($("#activeStemSpec")) {
    $("#activeStemSpec").textContent = `${t.sampleRate} · ${t.audioLength} · ${t.channels}`;
  }

  $("#detailStemTabs").innerHTML = t.stems
    .map(
      (s, idx) => `
    <button type="button" class="stem-tab stem-pill-btn ${idx === 0 ? "active" : ""}" data-stem-idx="${idx}">
      ${idx === 0 ? "● " : ""}${esc(s.name)}
    </button>
  `
    )
    .join("");

  if ($("#detailWaveBars")) {
    $("#detailWaveBars").innerHTML = buildWaveBars(t.id || t.title, 48, 66);
  }

  if ($("#detailResultSummary")) {
    $("#detailResultSummary").textContent =
      t.status === "失败"
        ? "本次任务在对齐阶段中断，保留原始输入快照"
        : t.status === "处理中"
          ? "引擎推理中，完成后自动归档至当前项目"
          : `输出 ${String(t.stems.length).padStart(2, "0")} · 已登记项目 Artifact（支持切换母带与独立干声分轨）`;
  }
  const stageDlBtn = $("#stageDownloadBtn");
  if (stageDlBtn) {
    if (primaryAudioStem) {
      stageDlBtn.disabled = false;
      stageDlBtn.dataset.downloadArtifact = primaryAudioStem.artifact_id;
      stageDlBtn.dataset.downloadName = primaryAudioStem.name;
    } else {
      stageDlBtn.disabled = true;
      delete stageDlBtn.dataset.downloadArtifact;
    }
  }
  if ($("#detailOutputMeta")) {
    $("#detailOutputMeta").innerHTML = `
      <span>${esc(t.modelLabel)}</span>
      <span>${esc(t.typeCode)}</span>
      <span>${esc(t.sampleRate)}</span>
      <span>${esc(t.channels)}</span>
      <span>${esc(t.audioLength)}</span>
      <span>${esc(t.format)}</span>
    `;
  }

  // Left .output-pane — 1. 提示词与编曲指令（Prompt）
  $("#detailPrompt").innerHTML = renderPromptWithChips(t);

  // Right .input-pane — 2. 结构化歌词 / LRC 时间轴
  if ($("#detailLyricsBadge")) {
    $("#detailLyricsBadge").textContent = t.lrc.length > 0 ? `1 / 1 份（已对齐 ${t.lrc.length} 行）` : "0 / 1 份";
    $("#detailLyricsBadge").classList.toggle("empty", t.lrc.length === 0);
  }
  if (t.lrc.length > 0) {
    $("#detailLrcList").className = "lrc-list lrc-sheet";
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
  } else {
    $("#detailLrcList").className = "lrc-list";
    $("#detailLrcList").innerHTML = `
      <div class="ref-empty-box">
        <span>未传入结构化歌词或 LRC 文本（0 / 1 份）</span>
        <small>不同模型与工序支持不同：ACE-Step 1.5 / Whisper Small 支持结构化歌词与逐词对齐；纯器乐配乐、UVR5 分轨与纯 RVC 干声转换无需歌词</small>
      </div>
    `;
  }

  // 3. 参考音频 / 输入分轨干声
  const maxAudios = 4;
  if ($("#detailRefAudioCount")) {
    $("#detailRefAudioCount").textContent = `${t.refAudios.length} / ${maxAudios} 轨 · 当前模型上限 ${maxAudios} 轨`;
    $("#detailRefAudioCount").classList.toggle("empty", t.refAudios.length === 0);
  }
  if ($("#detailRefAudioBody")) {
    if (t.refAudios.length > 0) {
      $("#detailRefAudioBody").innerHTML = `<div style="display:flex;flex-direction:column;gap:10px;">${t.refAudios
        .slice(0, 4)
        .map(
          (a, i) => `
        <div class="ref-audio-card ref-media-card" id="ref-aud-${i}" tabindex="-1">
          <div class="ref-audio-head">
            <div style="display:flex;align-items:center;gap:8px;min-width:0;">
              <span class="physics-pill">Audio ${i + 1}</span>
              <div style="min-width:0;">
                <strong style="font-size:12.5px;display:block;"><code>${esc(a.name)}</code></strong>
                <small class="tabular-nums" style="font-size:11px;color:var(--muted);">${esc(a.spec)}</small>
              </div>
            </div>
            <div class="ref-audio-wave" aria-hidden="true">
              <i style="height:8px"></i><i style="height:14px"></i><i style="height:18px"></i><i style="height:10px"></i><i style="height:16px"></i><i style="height:20px"></i><i style="height:12px"></i><i style="height:15px"></i><i style="height:9px"></i><i style="height:17px"></i><i style="height:11px"></i><i style="height:7px"></i>
            </div>
          </div>
          ${
            a.artifact_id
              ? `<audio controls preload="metadata" data-ref-audio-id="${esc(a.artifact_id)}" aria-label="Audio ${i + 1} ${esc(a.name)}"></audio>`
              : ""
          }
        </div>
      `
        )
        .join("")}</div>`;
      hydrateRefAudioPreviews();
    } else {
      $("#detailRefAudioBody").innerHTML = `
        <div class="ref-empty-box">
          <span>未传入参考音频或分轨干声（0 / 4 轨）</span>
          <small>不同模型支持上限不同：FFmpeg 母带混音最高支持 4 轨分轨输入，ACE-Step 1.5 支持 2 段参考旋律，RVC / UVR5 支持 1 轨源音频，纯文生 T2M 为 0 轨</small>
        </div>
      `;
    }
  }

  // 4. 角色声线模型（RVC Voice Model）
  const maxVoices = 1;
  if ($("#detailVoiceConfigBadge")) {
    $("#detailVoiceConfigBadge").textContent = `${t.voiceConfigs.length} / ${maxVoices} 个 · 当前模型上限 ${maxVoices} 个`;
    $("#detailVoiceConfigBadge").classList.toggle("empty", t.voiceConfigs.length === 0);
  }
  if ($("#detailVoiceConfigBody")) {
    if (t.voiceConfigs.length > 0) {
      $("#detailVoiceConfigBody").innerHTML = `<div style="display:flex;flex-direction:column;gap:10px;">${t.voiceConfigs
        .map(
          (vc, i) => `
        <div class="ref-audio-card ref-media-card" id="ref-voice-${i}" tabindex="-1">
          <div class="ref-audio-head">
            <div style="display:flex;align-items:center;gap:8px;min-width:0;">
              <span class="physics-pill">Voice ${i + 1}</span>
              <div style="min-width:0;">
                <strong style="font-size:13px;display:block;">🎙️ <code>${esc(vc.id)}</code> · ${esc(vc.role)}</strong>
                <small class="tabular-nums" style="font-size:11px;color:var(--muted);">${esc(vc.params)}</small>
              </div>
            </div>
            <span class="cap-pill"><strong>RVC v2</strong><small>已挂载</small></span>
          </div>
        </div>
      `
        )
        .join("")}</div>`;
    } else {
      $("#detailVoiceConfigBody").innerHTML = `
        <div class="ref-empty-box">
          <span>未挂载 RVC 角色声线模型（0 / 1 个）</span>
          <small>不同模型支持上限不同：本地 RVC v2 支持挂载预置 voice_model_id（如 mengsk-comfort-v2-20260901 / mengsk-demo-v1），原生编曲或分轨无需挂载</small>
        </div>
      `;
    }
  }

  // Bottom .inspect-tech
  $("#detailParams").innerHTML = `
    <span>任务 ID</span><span><code>${esc(t.id)}</code></span>
    <span>模型 × 渠道</span><span class="identity-channel">${esc(t.modelLabel)} × ${channelLogoFor(t.channelLabel)}${esc(t.channelLabel)}</span>
    <span>声学输出规格</span><span>${esc(t.typeCode)} (${esc(t.typeLabel)}) · 采样率 ${esc(t.sampleRate)} · 声道 ${esc(t.channels)} · 时长 ${esc(t.audioLength)} · 格式 ${esc(t.format)}</span>
    <span>四要素输入快照</span><span>1. 提示词 1/1（唯一） · 2. 歌词与LRC ${t.lrc.length ? "1/1 份" : "0/1 份"} · 3. 参考音频与分轨 ${t.refAudios.length}/4 轨（全局最大 4 轨） · 4. RVC 声线模型 ${t.voiceConfigs.length}/1 个</span>
    <span>创建时间与项目</span><span>${esc(t.createdAt)} · 所属项目 <code>${esc(t.project)}</code>${t.voiceModelId ? ` · 声线 <code>${esc(t.voiceModelId)}</code>` : ""}</span>
  `;

  $("#detailArtifacts").innerHTML = t.stems
    .map(
      (s, idx) => `
    <div class="mcp-tool-item">
      <span>${idx + 1}. <strong>${esc(s.name)}</strong> <small class="tabular-nums" style="color:var(--muted);">(${esc(s.spec)})</small></span>
      <button type="button" class="btn-outline" data-download-artifact="${esc(s.artifact_id)}" data-download-name="${esc(s.name)}">下载 ↓</button>
    </div>
  `
    )
    .join("");

  const totalFmt = formatTimeSec(t.durationSeconds || 0);
  $("#playerTime").textContent = `00:00 / ${totalFmt}`;
  selectAudioStem(t.stems[0]);
  $("#exportLrcBtn").disabled = !t.lrc.length && !t.rawLrc;
  notifyResize();
}

let currentRoute = location.hash.slice(1) || "tasks";

function syncRoute(nextRoute, notifyHost = true) {
  if (typeof nextRoute === "string" && nextRoute) {
    currentRoute = nextRoute;
    if (notifyHost) {
      const sdkPath =
        nextRoute === "models"
          ? "/models"
          : nextRoute === "mcp"
            ? "/mcp"
            : nextRoute.startsWith("task=")
              ? `/tasks/${nextRoute.slice(5)}`
              : "/";
      try {
        sdk?.navigation?.request?.({ target: "app", path: sdkPath })?.catch?.(() => {});
      } catch {}
    }
  } else if (location.hash.slice(1)) {
    currentRoute = location.hash.slice(1);
  }
  const rawHash = currentRoute || "tasks";

  if (rawHash.startsWith("task=")) {
    const taskId = decodeURIComponent(rawHash.slice(5));
    if ($("#appBreadcrumb")) $("#appBreadcrumb").hidden = false;
    if ($("#appHeader")) $("#appHeader").hidden = true;
    if ($("#appTabs")) $("#appTabs").hidden = true;
    $("#sectionTasks").hidden = true;
    $("#sectionModels").hidden = true;
    $("#sectionMcp").hidden = true;
    $("#sectionTaskDetail").hidden = false;
    renderTaskDetail(taskId);
    return;
  }

  resetAudioPreview();
  if ($("#titleEditBar")) $("#titleEditBar").hidden = true;

  if ($("#appBreadcrumb")) $("#appBreadcrumb").hidden = false;
  if ($("#appHeader")) $("#appHeader").hidden = false;
  if ($("#appTabs")) $("#appTabs").hidden = false;
  $("#sectionTaskDetail").hidden = true;

  const tab = ["tasks", "models", "mcp"].includes(rawHash) ? rawHash : "tasks";
  document.querySelectorAll(".app-tab").forEach((el) => {
    const active = el.dataset.tab === tab;
    el.classList.toggle("active", active);
    if (active) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });

  if ($("#crumbCurrentView")) {
    $("#crumbCurrentView").textContent = tab === "models" ? "模型" : tab === "mcp" ? "MCP" : "任务";
  }

  $("#sectionTasks").hidden = tab !== "tasks";
  $("#sectionModels").hidden = tab !== "models";
  $("#sectionMcp").hidden = tab !== "mcp";

  if (tab === "tasks") {
    renderTaskSection();
    if (savedTasksScrollY > 0) {
      const targetY = savedTasksScrollY;
      savedTasksScrollY = 0;
      requestAnimationFrame(() => window.scrollTo(0, targetY));
    }
  } else if (tab === "models") {
    renderModelsSection();
  } else if (tab === "mcp") {
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

    if (dashData?.app_version && $("#appVersionBadge")) {
      $("#appVersionBadge").textContent = `Music MCP · v${dashData.app_version}`;
    }
    if (Array.isArray(dashData?.tasks)) {
      tasks = dashData.tasks.map(normalizeTask);
      updateProjectFilterOptions();
    }
    if (Array.isArray(catalogData?.models) && catalogData.models.length > 0) {
      models = catalogData.models.map(enrichModel);
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
  const menuToggleBtn = e.target.closest("[data-menu-toggle]");
  if (menuToggleBtn) {
    e.preventDefault();
    e.stopPropagation();
    const wrap = menuToggleBtn.closest(".card-more-wrap");
    const menu = wrap?.querySelector(".card-action-menu");
    const wasOpen = menu && !menu.hidden;
    closeAllTaskMenus();
    if (menu && !wasOpen) {
      menu.hidden = false;
      menuToggleBtn.setAttribute("aria-expanded", "true");
    }
    return;
  }

  const taskActionBtn = e.target.closest("[data-task-action]");
  if (taskActionBtn) {
    e.preventDefault();
    e.stopPropagation();
    closeAllTaskMenus();
    const actionType = taskActionBtn.dataset.taskAction;
    const taskId = taskActionBtn.dataset.taskId;
    if (!taskId) return;
    const targetTask = tasks.find((x) => x.id === taskId);

    if (actionType === "edit") {
      const currentTitle = targetTask?.title || taskId;
      const rawTitle =
        typeof window.prompt === "function"
          ? window.prompt("请输入新的任务标题（1–80 字符）：", currentTitle)
          : currentTitle;
      if (rawTitle === null) return;
      const title = rawTitle.trim();
      if (!title) {
        showBanner("任务标题不能为空", true);
        return;
      }
      try {
        await apiRequest("POST", "/api/tasks/" + encodeURIComponent(taskId) + "/title", { title });
        await refreshDashboardData(false);
      } catch (err) {
        showBanner(`保存标题失败：${err.message || err}`, true);
      }
      return;
    }

    if (actionType === "toggle-nsfw") {
      const isNsfw = targetTask?.contentRating === "nsfw" || targetTask?.content_rating === "nsfw";
      const action = isNsfw ? "clear" : "mark";
      try {
        await apiRequest("POST", "/api/tasks/" + encodeURIComponent(taskId) + "/content-rating", {
          action,
          reason: action === "clear" ? "manual clear from card menu" : "manual mark from card menu",
        });
        await refreshDashboardData(false);
      } catch (err) {
        showBanner(`更新分级状态失败：${err.message || err}`, true);
      }
      return;
    }

    if (actionType === "delete") {
      const confirmed =
        typeof window.confirm === "function"
          ? window.confirm(`确认删除任务「${targetTask?.title || taskId}」？`)
          : true;
      if (!confirmed) return;
      try {
        await apiRequest("POST", "/api/tasks/" + encodeURIComponent(taskId) + "/delete", {});
        await refreshDashboardData(false);
      } catch (err) {
        showBanner(`删除任务失败：${err.message || err}`, true);
      }
      return;
    }
    return;
  }

  if (e.target.closest(".card-action-menu")) {
    e.stopPropagation();
    return;
  }

  closeAllTaskMenus();

  const tabLink = e.target.closest(".app-tab[data-tab]");
  if (tabLink) {
    e.preventDefault();
    syncRoute(tabLink.dataset.tab);
    return;
  }

  const navBackLink = e.target.closest("[data-nav-back]");
  if (navBackLink) {
    e.preventDefault();
    syncRoute(navBackLink.dataset.navBack || "tasks");
    return;
  }

  const openTaskLink = e.target.closest("[data-open-task]");
  if (openTaskLink) {
    e.preventDefault();
    savedTasksScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    syncRoute(`task=${encodeURIComponent(openTaskLink.dataset.openTask)}`);
    return;
  }

  const focusRefBtn = e.target.closest("[data-focus-ref]");
  if (focusRefBtn) {
    const targetId = focusRefBtn.dataset.focusRef;
    const targetEl = document.getElementById(targetId);
    if (targetEl) {
      document.querySelectorAll(".reference-focus").forEach((el) => el.classList.remove("reference-focus"));
      targetEl.classList.add("reference-focus");
      targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => targetEl.classList.remove("reference-focus"), 1800);
    }
    return;
  }

  const stBtn = e.target.closest("[data-status]");
  if (stBtn) {
    currentStatus = stBtn.dataset.status;
    renderTaskSection();
    return;
  }

  const mChBtn = e.target.closest("[data-model-channel]");
  if (mChBtn) {
    currentModelChannel = mChBtn.dataset.modelChannel || "全部";
    renderModelsSection();
    return;
  }

  const vmBtn = e.target.closest("[data-view-mode]");
  if (vmBtn) {
    currentViewMode = vmBtn.dataset.viewMode;
    document.querySelectorAll("[data-view-mode]").forEach((b) => b.classList.toggle("active", b === vmBtn));
    renderTaskSection();
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
    if (stem) {
      if ($("#activeStemTitle")) {
        $("#activeStemTitle").textContent = stem.name;
      }
      selectAudioStem(stem);
    }
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

  if (e.target.closest("#playToggleBtn, #detailWaveBars")) {
    await toggleAudioPlayback();
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
    $("#editTitleBtn")?.focus();
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
        if ($("#detailShellTitle")) {
          $("#detailShellTitle").textContent = updated.title;
        }
        if ($("#crumbCurrentView")) {
          $("#crumbCurrentView").innerHTML = `<a href="#tasks" data-nav-back="tasks">任务</a> <span aria-hidden="true">/</span> <span aria-current="page">${esc(updated.title)}</span>`;
        }
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

// Keyboard support: Enter/Space on whole-card or whole-row [data-open-task] & Roving Tabindex
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeAllTaskMenus();
  }
  if (e.target.closest(".card-more-wrap")) {
    return;
  }
  const openTarget = e.target.closest("[data-open-task]");
  if (openTarget && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    savedTasksScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    syncRoute(`task=${encodeURIComponent(openTarget.dataset.openTask)}`);
    return;
  }

  const grid = e.target.closest('[data-roving-grid="true"]');
  if (grid && ["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(e.key)) {
    const items = Array.from(grid.querySelectorAll(".roving-item"));
    const curIdx = items.indexOf(e.target.closest(".roving-item"));
    if (curIdx < 0) return;
    e.preventDefault();
    let nextIdx = curIdx;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") nextIdx = Math.min(items.length - 1, curIdx + 1);
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") nextIdx = Math.max(0, curIdx - 1);
    items.forEach((it, idx) => it.setAttribute("tabindex", idx === nextIdx ? "0" : "-1"));
    items[nextIdx]?.focus();
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
      const ratio = Math.min(1, cur / dur);
      $("#waveProgress").style.width = `${(ratio * 100).toFixed(1)}%`;
      const bars = Array.from(document.querySelectorAll("#detailWaveBars .wave-bar"));
      const playedCount = Math.round(ratio * bars.length);
      bars.forEach((b, i) => b.classList.toggle("played", i < playedCount));
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
    $("#playerStatusText").textContent = "播放结束，点击重播";
  });
  audioEl.addEventListener("error", () => {
    $("#playToggleBtn").textContent = "▶ 播放";
    $("#playerStatusText").textContent = "音频无法播放，请重试或下载原音频";
  });
}

function handleWaveSeek(e) {
  const audio = $("#audioElement");
  if (!audio || !audio.src) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = audio.duration || currentDetailTask?.durationSeconds || 0;
  if (dur > 0) {
    audio.currentTime = ratio * dur;
  }
}

$("#waveTrack")?.addEventListener("click", handleWaveSeek);
window.addEventListener("pagehide", resetAudioPreview);

const searchEl = $("#searchInput");
if (searchEl) {
  searchEl.addEventListener("compositionstart", () => {
    isComposing = true;
    const b = $("#imeBadge");
    if (b) { b.textContent = "IME 组合中"; b.classList.add("composing"); }
  });
  searchEl.addEventListener("compositionend", () => {
    isComposing = false;
    const b = $("#imeBadge");
    if (b) { b.textContent = "CJK 守卫"; b.classList.remove("composing"); }
    renderTaskSection();
  });
  searchEl.addEventListener("input", () => {
    if (!isComposing) renderTaskSection();
  });
}
$("#channelFilter")?.addEventListener("change", renderTaskSection);
$("#projectFilter")?.addEventListener("change", renderTaskSection);
$("#modelSearchInput")?.addEventListener("input", renderModelsSection);
$("#modelBizSelect")?.addEventListener("change", renderModelsSection);
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
  if (isPageActive && (!currentRoute || currentRoute === "tasks")) {
    refreshDashboardData(true);
  }
}, 10000);

