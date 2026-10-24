from __future__ import annotations

import audioop
import base64
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import wave

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .artifacts import ArtifactError, ArtifactNotFound, ArtifactStore
from .content_rating import ContentRatingError, include_nsfw_from_headers
from .dashboard_assets import DashboardAssets, load_or_build_assets
from .executor import ExecutionError, MusicExecutor
from .registry import EtcdRegistry
from .tasks import InvalidTaskTitle, TaskNotFound, TaskStore

MAX_PREVIEW_RAW_BYTES = 2_200_000  # ~2.93 MiB base64, safely under Studio 4 MiB JSON limit
CHUNK_RAW_BYTES = 1_572_864  # 1.5 MiB raw per chunk (~2.1 MiB base64 JSON)


def _json_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _error_response(code: str, status_code: int = 400) -> JSONResponse:
    # Studio RegisteredSource.Request requires {"code": "^[A-Z][A-Z0-9_]{0,63}$"} on >= 400
    return _json_response({"code": code, "error": code.lower()}, status_code=status_code)


def _downsample_wav_for_preview(wav_bytes: bytes, max_bytes: int = MAX_PREVIEW_RAW_BYTES) -> bytes:
    if len(wav_bytes) <= max_bytes:
        return wav_bytes
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            nchannels = src.getnchannels()
            sampwidth = src.getsampwidth()
            framerate = src.getframerate()
            frames = src.readframes(src.getnframes())
        if nchannels > 1:
            frames = audioop.tomono(frames, sampwidth, 0.5, 0.5)
            nchannels = 1
        if sampwidth != 2:
            frames = audioop.lin2lin(frames, sampwidth, 2)
            sampwidth = 2
        target_rate = min(framerate, 16000)
        if framerate != target_rate:
            frames, _ = audioop.ratecv(frames, sampwidth, 1, framerate, target_rate, None)
        max_pcm = max_bytes - 128
        if len(frames) > max_pcm:
            frames = frames[: (max_pcm // 2) * 2]
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(target_rate)
            dst.writeframes(frames)
        return out.getvalue()
    except Exception:
        return wav_bytes[:max_bytes]


def build_audio_preview_data_url(content_path: Path, media_type: str, artifact_id: str) -> tuple[str, str]:
    raw_size = content_path.stat().st_size
    if raw_size <= MAX_PREVIEW_RAW_BYTES:
        b64 = base64.b64encode(content_path.read_bytes()).decode("ascii")
        return media_type, f"data:{media_type};base64,{b64}"

    cache_dir = Path(tempfile.gettempdir()) / "music-mcp-previews"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_mp3 = cache_dir / f"{artifact_id}.mp3"
    if cached_mp3.is_file() and 0 < cached_mp3.stat().st_size <= MAX_PREVIEW_RAW_BYTES:
        b64 = base64.b64encode(cached_mp3.read_bytes()).decode("ascii")
        return "audio/mpeg", f"data:audio/mpeg;base64,{b64}"

    if shutil.which("ffmpeg"):
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(content_path),
                "-vn",
                "-ac",
                "2",
                "-ar",
                "32000",
                "-b:a",
                "96k",
                "-f",
                "mp3",
                str(cached_mp3),
            ],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and cached_mp3.is_file() and cached_mp3.stat().st_size <= MAX_PREVIEW_RAW_BYTES:
            b64 = base64.b64encode(cached_mp3.read_bytes()).decode("ascii")
            return "audio/mpeg", f"data:audio/mpeg;base64,{b64}"

    wav_preview = _downsample_wav_for_preview(content_path.read_bytes(), MAX_PREVIEW_RAW_BYTES)
    b64 = base64.b64encode(wav_preview).decode("ascii")
    return "audio/wav", f"data:audio/wav;base64,{b64}"


class MusicDashboard:
    def __init__(
        self,
        store: ArtifactStore,
        tasks: TaskStore,
        executor: MusicExecutor,
        registry: EtcdRegistry,
        tools_schema: list[dict[str, Any]],
        assets: DashboardAssets | None = None,
    ) -> None:
        self.store = store
        self.tasks = tasks
        self.executor = executor
        self.registry = registry
        self.tools_schema = tools_schema
        self.assets = assets if assets is not None else load_or_build_assets(__version__)

    def _probe_services(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        service_defs = [
            ("music3", "music-minimax-music3-api", self.executor.service_urls.music3, "resident", "MiniMax Music 3"),
            ("ace_step", "music-ace-step-api", self.executor.service_urls.music_editor, "on_demand", "ACE-Step 1.5"),
            ("uvr5", "music-uvr5-api", self.executor.service_urls.uvr5, "resident", "UVR5 MelBand RoFormer"),
            ("rvc", "music-rvc-api", self.executor.service_urls.rvc, "resident", "RVC v2 Voice Conversion"),
            ("lyrics_aligner", "music-lyrics-aligner-api", self.executor.service_urls.lyrics_aligner, "resident", "Stable Whisper Aligner"),
            ("mixer", "music-audio-mixer-api", self.executor.service_urls.mixer, "resident", "Pedalboard + FFmpeg Mixer"),
        ]
        services: list[dict[str, Any]] = []
        for key, k8s_name, url, mode, label in service_defs:
            if not url:
                services.append(
                    {
                        "key": key,
                        "service": k8s_name,
                        "label": label,
                        "mode": mode,
                        "ready": False,
                        "status": "on_demand" if mode == "on_demand" else "not_configured",
                    }
                )
                continue
            try:
                headers = self.executor._music_editor_headers() if key == "ace_step" else {}
                resp = self.executor.client.get(f"{url}/health", headers=headers, timeout=2.5)
                ready = 200 <= resp.status_code < 300
                status = "ready" if ready else ("on_demand" if mode == "on_demand" else "degraded")
            except Exception:
                ready = False
                status = "on_demand" if mode == "on_demand" else "offline"
            services.append(
                {
                    "key": key,
                    "service": k8s_name,
                    "label": label,
                    "mode": mode,
                    "ready": ready,
                    "status": status,
                }
            )

        voice_models: list[dict[str, Any]] = [
            {
                "id": "mengsk-comfort-v2-20260901",
                "label": "孟斯克 · 舒适声线 v2 (主力)",
                "epochs": 200,
                "sample_rate": "40kHz",
                "f0_method": "rmvpe",
                "status": "ready",
            },
            {
                "id": "mengsk-demo-v1",
                "label": "孟斯克 · 初始样本 v1",
                "epochs": 200,
                "sample_rate": "40kHz",
                "f0_method": "rmvpe",
                "status": "ready",
            },
        ]
        try:
            rvc_resp = self.executor.client.get(f"{self.executor.service_urls.rvc}/v1/voice-models", timeout=2.5)
            if rvc_resp.status_code == 200:
                data = rvc_resp.json().get("data", [])
                if isinstance(data, list) and data:
                    known_ids = {v["id"] for v in voice_models}
                    for item in data:
                        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] not in known_ids:
                            voice_models.append(
                                {
                                    "id": item["id"],
                                    "label": item["id"],
                                    "epochs": 200,
                                    "sample_rate": "40kHz",
                                    "f0_method": "rmvpe",
                                    "status": "ready",
                                }
                            )
        except Exception:
            pass

        return services, voice_models

    def build_model_catalog(self) -> dict[str, Any]:
        services, voice_models = self._probe_services()
        svc_map = {s["key"]: s for s in services}

        def st(key: str, default: str = "ready") -> str:
            return svc_map.get(key, {}).get("status", default)

        channels = [
            {
                "channel_id": "local",
                "title": "本地 · Local 5090",
                "badge": "本地常驻 / 按需",
                "badge_tone": "ready",
                "subtitle": "6 个本地模型与声线资产 · local-nvme Retain 持久化卷",
                "models": [
                    {
                        "model_id": "minimax-music3",
                        "title": "MiniMax Music 3",
                        "version": "v0.1.2 · SGLang-Omni 0.1.3",
                        "brand": "minimax",
                        "status": st("music3", "ready"),
                        "status_label": "Ready · 双卡可见 M1" if st("music3") == "ready" else "Standby · M1 双卡可见",
                        "description": "主力全曲候选生成模型。输出 32kHz / 16-bit 双声道 PCM WAV，保留自然尾奏，不强制截断。",
                        "capabilities": ["music.generate", "workflow.preflight"],
                        "specs": [
                            {"label": "推理框架", "value": "SGLang-Omni 0.1.3"},
                            {"label": "输出规格", "value": "32kHz · 16-bit Stereo"},
                            {"label": "GPU 策略", "value": "方案 M1 (双卡可见 0.50)"},
                            {"label": "单曲上限", "value": "200s 自然收尾"},
                        ],
                        "storage_pvc": "music3-models (/data/models/music/music3)",
                        "revision": "fbdf52fbaaca7995",
                    },
                    {
                        "model_id": "ace-step-v1-5",
                        "title": "ACE-Step 1.5",
                        "version": "v0.1.3 · Text2Music & Repaint",
                        "brand": "acestep",
                        "status": "on_demand" if not svc_map.get("ace_step", {}).get("ready") else "ready",
                        "status_label": "Ready · 在线" if svc_map.get("ace_step", {}).get("ready") else "On-Demand · 副本 0 按需唤醒",
                        "description": "负责局部区间条件重绘（music.redraw）与男女声分轨合唱生成（duet.*），重绘区间外保持源 PCM 帧零损拼接。",
                        "capabilities": ["music.redraw", "duet.plan", "duet.instrumental", "duet.vocal", "duet.preview"],
                        "specs": [
                            {"label": "基础模型", "value": "acestep-v15-base"},
                            {"label": "重绘契约", "value": "ace_step_v1_5_repaint"},
                            {"label": "合唱契约", "value": "text2music_lego"},
                            {"label": "常驻策略", "value": "按需唤醒 (replicas: 0)"},
                        ],
                        "storage_pvc": "music-ace-step-data (/data/models/music/ace-step)",
                        "revision": "acestep-v15-base",
                    },
                    {
                        "model_id": "uvr5-melband-roformer",
                        "title": "UVR5 MelBand RoFormer",
                        "version": "v0.1.9 · Big Beta4 + Dereverb",
                        "brand": "uvr5",
                        "status": st("uvr5", "ready"),
                        "status_label": "Ready · 常驻就绪" if st("uvr5") == "ready" else "Loading · 模型校验",
                        "description": "高精度伴奏与人声分离及独立去混响管线，输出 48kHz / 24-bit 伴奏轨、干声轨、湿声轨与混响残差（可选和声分离需显式配置 UVR5_BACKING_MODEL_FILENAME）。",
                        "capabilities": ["stems.separate"],
                        "specs": [
                            {"label": "分轨主模型", "value": "melband_roformer_big_beta4"},
                            {"label": "去混响模型", "value": "dereverb_anvuew_sdr_19.17"},
                            {"label": "输出规格", "value": "48kHz · 24-bit WAV"},
                            {"label": "分轨产物", "value": "伴奏 + 干声 + 诊断轨"},
                        ],
                        "storage_pvc": "uvr5-models (/data/models/music/uvr5)",
                        "revision": "sha256:700a9bd3831d4f7f",
                    },
                    {
                        "model_id": "rvc-v2-voice-conversion",
                        "title": "RVC v2 音色转换与训练引擎",
                        "version": "v0.2.3 · HuBERT + RMVPE 40k",
                        "brand": "rvc",
                        "status": st("rvc", "ready"),
                        "status_label": "Ready · 常驻就绪" if st("rvc") == "ready" else "Loading · 模型校验",
                        "description": "提供确定性录音切片预处理、RVC v2 模型训练及带检索率/清辅音保护的干声音色转换。",
                        "capabilities": ["voice.prepare", "voice.train", "voice.convert"],
                        "specs": [
                            {"label": "特征提取", "value": "hubert_base + rmvpe.pt"},
                            {"label": "底模采样率", "value": "pretrained_v2 40kHz"},
                            {"label": "默认检索率", "value": "index_rate = 0.66"},
                            {"label": "清辅音保护", "value": "protect = 0.33"},
                        ],
                        "storage_pvc": "rvc-models (/data/models/music/rvc/runtime)",
                        "revision": "sha256:f54b40fd2802423a",
                    },
                    {
                        "model_id": "mengsk-trained-voices",
                        "title": "Mengsk Trained Voices · 孟斯克声线包",
                        "version": f"{len(voice_models)} 个已验证声线模型",
                        "brand": "voice",
                        "status": "ready",
                        "status_label": f"Verified · {len(voice_models)} 套声线就绪",
                        "description": "从成都生产验证环境完整迁移并经 SHA-256 校验的孟斯克专属训练声线资产（含 model.pth 与 FAISS model.index）。",
                        "capabilities": ["voice.convert", "workflow.preflight"],
                        "specs": [
                            {"label": "主力声线", "value": "mengsk-comfort-v2-20260901"},
                            {"label": "备选声线", "value": "mengsk-demo-v1"},
                            {"label": "权重校验", "value": "sha256:8cceaabd0fb7dbc6"},
                            {"label": "索引校验", "value": "sha256:9fe5181390bf4254"},
                        ],
                        "storage_pvc": "rvc-models (/data/models/music/rvc/voices)",
                        "revision": "sha256:8cceaabd0fb7dbc6",
                        "voice_models": voice_models,
                    },
                    {
                        "model_id": "stable-whisper-lyrics-aligner",
                        "title": "Stable Whisper Lyrics Aligner + Production DSP Mixer",
                        "version": "v0.1.3 Aligner · v0.1.2 Mixer",
                        "brand": "whisper",
                        "status": "ready" if st("lyrics_aligner") == "ready" or st("mixer") == "ready" else "offline",
                        "status_label": (
                            f"Aligner: {st('lyrics_aligner').upper()} · Mixer: {st('mixer').upper()}"
                        ),
                        "description": "使用 stable-whisper (Whisper Small) 将已批准中文（zh）歌词逐行强制对齐到演唱干声生成毫秒级 Aligned_Lyrics.lrc，并经 Pedalboard + FFmpeg + WORLD / VF Synth / Production DSP 输出修音、合成器分轨、多轨预混及 -14 LUFS 母带 WAV 与 320k MP3。",
                        "capabilities": [
                            "lyrics.align",
                            "mix.master",
                            "voice.tune",
                            "voice.process",
                            "music.instrument_render",
                            "mix.render",
                        ],
                        "specs": [
                            {"label": "对齐模型", "value": "Whisper small.pt (20250625 · zh)"},
                            {"label": "对齐框架", "value": "stable-whisper 2.19.1"},
                            {"label": "DSP / 混音", "value": "Pedalboard + FFmpeg + WORLD + VF Synth"},
                            {"label": "交付规格", "value": "Master WAV + 320k MP3 + Aligned_Lyrics.lrc"},
                        ],
                        "storage_pvc": "lyrics-aligner-models (/data/models/music/lyrics-aligner)",
                        "revision": "sha256:9ecf779972d90ba4",
                    },
                ],
            },
        ]
        flat_models = [
            {
                "id": "local-minimax-music3",
                "channel": "本地",
                "biz": "音乐生成",
                "title": "MiniMax Music 3",
                "modelId": "minimax-music-3-omni-local",
                "status": st("music3", "ready"),
                "desc": "全曲文生音乐候选生成，输出 32kHz / 16-bit 双声道 WAV 并保留自然尾奏，适合主歌/副歌完整结构编曲与器乐铺底。",
                "specLine": "32kHz WAV · 15–200 秒",
                "caps": ["T2M · 文生音乐"],
                "refQuota": "无需参考音频 · 纯文本 Prompt + 歌词驱动",
                "maxSize": "≤ 50MB",
                "outputSpecs": ["32kHz", "≤ 200S", "WAV"],
                "tools": ["music.generate"],
                "teamScope": "文生音乐 (T2M) · 32kHz WAV · 15–200 秒",
                "vendorScope": "sglang-omni (Qwen-7B + DiT/DAV + VAE) · 支持中英文歌词结构控制",
                "voices": [],
            },
            {
                "id": "local-ace-step-15",
                "channel": "本地",
                "biz": "双人对唱",
                "title": "ACE-Step 1.5",
                "modelId": "acestep-v15-base-local",
                "status": "on_demand" if not svc_map.get("ace_step", {}).get("ready") else "ready",
                "desc": "负责局部区间条件重绘（music.redraw，重绘区间 ≤ 60s）与双人对唱分轨编排（duet.instrumental 无需参考音频，duet.vocal 接收 1 轨伴奏生成单轨人声），支持按需唤醒推理。",
                "specLine": "48kHz WAV · 歌词结构驱动 · 重绘 ≤ 60s",
                "caps": ["DUET · 双人对唱", "R2M · 局部重绘"],
                "refQuota": "duet.instrumental 无需参考音频 / duet.vocal 接收 1 轨伴奏生成单轨人声 / music.redraw 接收 1 轨源音频（重绘区间 ≤ 60s）",
                "maxSize": "≤ 50MB",
                "outputSpecs": ["48kHz", "30–240S", "WAV"],
                "tools": [
                    "music.redraw",
                    "music.conditioning_check",
                    "music.extend_resume",
                    "duet.plan",
                    "duet.instrumental",
                    "duet.vocal",
                    "duet.preview",
                ],
                "teamScope": "局部时间窗重绘 (R2M, ≤ 60s)、双人分唱蓝图规划、统一伴奏主干与分角色单轨干声生成 (DUET) · 48kHz WAV",
                "vendorScope": "ACE-Step v1.5 Base · 多段落歌词驱动编曲与局部条件重绘",
                "voices": [],
            },
            {
                "id": "local-uvr5-roformer",
                "channel": "本地",
                "biz": "分轨分离",
                "title": "UVR5 RoFormer",
                "modelId": "mel-band-roformer-big-beta4",
                "status": st("uvr5", "ready"),
                "desc": "高保真伴奏与人声干声分离及独立去混响管线，输出 48kHz / 24-bit WAV 伴奏、干声、湿声与混响残差四轨（可选和声分离需显式配置 UVR5_BACKING_MODEL_FILENAME）。",
                "specLine": "48kHz / 24-bit WAV · 伴奏/人声/去混响",
                "caps": ["STEM · 分轨分离", "Dereverb · 去混响"],
                "refQuota": "输入整曲音频 1 轨（分离 48kHz / 24-bit 伴奏 + 干声 + 诊断轨）",
                "maxSize": "≤ 50MB",
                "outputSpecs": ["48kHz", "≤ 600S", "WAV"],
                "tools": ["stems.separate"],
                "teamScope": "伴奏/人声分离与独立混响剥离 (STEM) · 48kHz / 24-bit WAV",
                "vendorScope": "MelBand RoFormer Big Beta4 + Dereverb 级联管线（可选和声分离受 UVR5_BACKING_MODEL_FILENAME 控制）",
                "voices": [],
            },
            {
                "id": "local-rvc-v2",
                "channel": "本地",
                "biz": "声线转换",
                "title": "RVC v2",
                "modelId": "rvc-v2-hubert-rmvpe-40k",
                "status": st("rvc", "ready"),
                "desc": "角色声线转换与人声音色权重训练，内置已安装角色声线模型库，默认用于主唱干声替换。",
                "specLine": f"40kHz / rmvpe · 已安装 {len(voice_models)} 个人声音色",
                "caps": ["RVC · 声线转换", "RMVPE · 基频提取"],
                "refQuota": "输入洁净人声干声 1–2 轨",
                "maxSize": "≤ 50MB",
                "outputSpecs": ["40kHz", "≤ 600S", "WAV"],
                "tools": ["voice.convert", "voice.prepare", "voice.train"],
                "teamScope": "角色声线转换、训练集切片清洗与 RVC 权重训练 (RVC) · 40kHz WAV",
                "vendorScope": "Retrieval-based Voice Conversion v2 (HuBERT + RMVPE 基频提取)",
                "voices": [
                    {
                        "id": v["id"],
                        "spec": f"{v.get('sample_rate', '40kHz')} · {v.get('f0_method', 'rmvpe')} · {'生产主推' if idx == 0 else '历史基线'}",
                    }
                    for idx, v in enumerate(voice_models)
                ],
            },
            {
                "id": "local-whisper-small",
                "channel": "本地",
                "biz": "歌词对齐",
                "title": "stable-whisper (Whisper Small)",
                "modelId": "stable-whisper-small-align",
                "status": st("lyrics_aligner", "ready"),
                "desc": "基于 stable-whisper (Whisper Small) 将已批准中文（zh）歌词文本与人声干声逐行强制对齐，导出标准行级 Aligned_Lyrics.lrc 时间轴产物。",
                "specLine": "Aligned_Lyrics.lrc · 中文 (zh) 行级强制对齐",
                "caps": ["LRC · 中文强制对齐 (zh)"],
                "refQuota": "人声干声 1 轨 + 中文（zh）净版歌词文本 1 份",
                "maxSize": "≤ 50MB",
                "outputSpecs": ["16kHz+", "≤ 600S", "LRC (zh)"],
                "tools": ["lyrics.align"],
                "teamScope": "人声干声与中文（zh）净版歌词强制时间轴对齐 (LRC) · 导出 Aligned_Lyrics.lrc",
                "vendorScope": "stable-whisper (Whisper Small) 中文行级强制对齐管线",
                "voices": [],
            },
            {
                "id": "local-pedalboard-mixer",
                "channel": "本地",
                "biz": "母带混音",
                "title": "Pedalboard + Production DSP",
                "modelId": "pedalboard-ffmpeg-loudnorm",
                "status": st("mixer", "ready"),
                "desc": "集成 WORLD 独立音高修音、VF Synth 合成器分轨渲染、Production DSP 人声效果链与多轨预混，以及 Pedalboard + FFmpeg -14 LUFS 母带响度归一交付。",
                "specLine": "48kHz WAV · WORLD / VF Synth / Production DSP / -14 LUFS",
                "caps": ["MIX · 多轨混音", "-14 LUFS · 母带归一", "WORLD · 独立修音", "VF Synth · 合成器渲染", "Production DSP · 效果链"],
                "refQuota": "伴奏轨 1 轨 + 角色人声干声 1–2 轨（预混最高 16 轨）",
                "maxSize": "≤ 50MB",
                "outputSpecs": ["48kHz", "≤ 600S", "WAV"],
                "tools": [
                    "mix.master",
                    "mix.render",
                    "voice.tune",
                    "voice.process",
                    "music.instrument_render",
                    "music.production_capabilities",
                    "music.production_status",
                    "music.production_cancel",
                ],
                "teamScope": "WORLD 独立修音、VF Synth 合成器渲染、Production DSP 效果链/多轨预混与 -14 LUFS 母带交付 (MIX)",
                "vendorScope": "Spotify Pedalboard + ffmpeg EBU R128 Loudnorm + WORLD + VF Synth + Production DSP",
                "voices": [],
            },
        ]
        return {
            "schema_version": 1,
            "app_version": __version__,
            "models": flat_models,
            "channels": channels,
            "voice_models": voice_models,
        }

    def build_mcp_status(self) -> dict[str, Any]:
        services, voice_models = self._probe_services()
        reg = self.registry.status_snapshot()
        return {
            "schema_version": 1,
            "app_id": "music-mcp",
            "app_version": __version__,
            "gateway": {
                "external_entry": "STUDIO_MCP_URL (Single Studio Gateway)",
                "protocol": "MCP Streamable HTTP (Bearer Authenticated)",
                **reg,
            },
            "services": services,
            "voice_models": voice_models,
            "tools": self.tools_schema,
            "content_rating": {
                "supported": True,
                "tool": "music.rate_content",
                "rubric_version": "rubric v1.0",
                "add_only": True,
            },
        }

    def _active_store(self, request: Request | None = None) -> ArtifactStore:
        if request is not None:
            state_store = getattr(request.app.state, "artifacts", None) or getattr(request.app.state, "store", None)
            if state_store is not None:
                return state_store
        return self.store

    def _active_tasks(self, request: Request | None = None) -> TaskStore:
        if request is not None:
            state_tasks = getattr(request.app.state, "tasks", None) or getattr(request.app.state, "task_store", None)
            if state_tasks is not None:
                state_ratings = getattr(request.app.state, "ratings", None)
                if state_ratings is not None:
                    state_tasks.ratings = state_ratings
                return state_tasks
        return self.tasks

    def build_Dashboard_Summary_For(self, tasks_store: TaskStore, *, include_nsfw: bool = False) -> dict[str, Any]:
        task_items = tasks_store.list_tasks(include_nsfw=include_nsfw, include_ui_fields=True)
        services, voice_models = self._probe_services()
        reg = self.registry.status_snapshot()
        all_artifacts_count = sum(len(t.get("artifacts", [])) for t in task_items)
        ready_services = sum(1 for s in services if s["ready"])

        return {
            "schema_version": 1,
            "app_id": "music-mcp",
            "app_version": __version__,
            "updated_at": datetime.now(UTC).isoformat(),
            "kpis": {
                "mcp_gateway": {
                    "status": "ready",
                    "registered": reg["registered"],
                    "tool_count": len(self.tools_schema),
                    "domain": "music",
                },
                "engines": {
                    "ready_count": ready_services,
                    "total_count": len(services),
                    "services": services,
                },
                "voices": {
                    "count": len(voice_models),
                    "primary_voice": "mengsk-comfort-v2-20260901",
                    "models": voice_models,
                },
                "storage": {
                    "task_count": len(task_items),
                    "artifact_count": all_artifacts_count,
                    "pvc": "music-projects (local-nvme Retain)",
                },
            },
            "tasks": task_items,
        }

    def build_dashboard_summary(self, *, include_nsfw: bool = False) -> dict[str, Any]:
        return self.build_Dashboard_Summary_For(self.tasks, include_nsfw=include_nsfw)

    async def handle_internal(self, request: Request) -> Response:
        if self.assets is None:
            return _error_response("DASHBOARD_UNAVAILABLE", 503)
        path = request.url.path
        if path.endswith("/internal/dashboard-declaration"):
            return Response(
                self.assets.declaration,
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if path.endswith("/internal/dashboard-index"):
            return Response(
                self.assets.index,
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        asset_name = request.path_params.get("asset", "")
        try:
            data, mime = self.assets.asset(asset_name)
        except (KeyError, ValueError, OSError):
            return _error_response("DASHBOARD_ASSET_UNAVAILABLE", 404)
        return Response(
            data,
            headers={
                "Content-Type": mime,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_standalone_page(self, request: Request) -> Response:
        if self.assets is None:
            return _error_response("DASHBOARD_UNAVAILABLE", 503)
        asset_name = request.path_params.get("asset", "index.html") or "index.html"
        try:
            data, mime = self.assets.asset(asset_name)
        except (KeyError, ValueError, OSError):
            return _error_response("DASHBOARD_ASSET_UNAVAILABLE", 404)
        return Response(
            data,
            headers={
                "Content-Type": mime,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_api_dashboard(self, request: Request) -> JSONResponse:
        include_nsfw = include_nsfw_from_headers(request.headers)
        tasks_store = self._active_tasks(request)
        summary = await run_in_threadpool(
            lambda: self.build_Dashboard_Summary_For(tasks_store, include_nsfw=include_nsfw)
        )
        return _json_response(summary)

    async def handle_api_list_tasks(self, request: Request) -> JSONResponse:
        include_nsfw = include_nsfw_from_headers(request.headers)
        tasks_store = self._active_tasks(request)
        project_id = request.query_params.get("project_id") or None
        operation = request.query_params.get("operation") or request.query_params.get("workflow") or None
        status = request.query_params.get("status") or None
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
            offset = max(0, int(request.query_params.get("offset", "0")))
        except ValueError:
            limit, offset = 50, 0
        items, total = await run_in_threadpool(
            lambda: tasks_store.query_tasks(
                project_id=project_id,
                operation=operation,
                status=status,
                limit=limit,
                offset=offset,
                include_nsfw=include_nsfw,
                include_ui_fields=True,
            )
        )
        return _json_response(
            {
                "tasks": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    async def handle_api_task_stats(self, request: Request) -> JSONResponse:
        include_nsfw = include_nsfw_from_headers(request.headers)
        tasks_store = self._active_tasks(request)
        project_id = request.query_params.get("project_id") or None
        stats = await run_in_threadpool(
            lambda: tasks_store.get_stats(include_nsfw=include_nsfw, project_id=project_id)
        )
        return _json_response(stats)

    async def handle_api_task_detail(self, request: Request) -> JSONResponse:
        task_id = request.path_params.get("task_id", "")
        include_nsfw = include_nsfw_from_headers(request.headers)
        tasks_store = self._active_tasks(request)
        try:
            task = await run_in_threadpool(
                lambda: tasks_store.get_task(
                    task_id,
                    include_nsfw=include_nsfw,
                    include_ui_fields=True,
                )
            )
        except TaskNotFound:
            return _error_response("TASK_NOT_FOUND", 404)
        return _json_response({"task": task})

    async def handle_api_task_title(self, request: Request) -> JSONResponse:
        task_id = request.path_params.get("task_id", "")
        tasks_store = self._active_tasks(request)
        try:
            raw = await request.body()
            if len(raw) > 8192:
                return _error_response("REQUEST_TOO_LARGE", 400)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("title"), str):
                return _error_response("INVALID_TITLE", 400)
            updated = await run_in_threadpool(tasks_store.rename_task, task_id, payload["title"])
        except TaskNotFound:
            return _error_response("TASK_NOT_FOUND", 404)
        except (InvalidTaskTitle, ValueError, UnicodeDecodeError):
            return _error_response("INVALID_TITLE", 400)
        return _json_response({"task": updated})

    async def handle_api_task_delete(self, request: Request) -> JSONResponse:
        task_id = request.path_params.get("task_id", "")
        tasks_store = self._active_tasks(request)
        try:
            res = await run_in_threadpool(tasks_store.delete_task, task_id)
        except TaskNotFound:
            return _error_response("TASK_NOT_FOUND", 404)
        return _json_response(res)

    async def handle_api_task_content_rating(self, request: Request) -> JSONResponse:
        task_id = request.path_params.get("task_id", "")
        tasks_store = self._active_tasks(request)
        actor = (
            request.headers.get("x-user-id")
            or request.headers.get("x-studio-user-id")
            or "studio-user"
        )
        try:
            raw = await request.body()
            if len(raw) > 8192:
                return _error_response("REQUEST_TOO_LARGE", 400)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                return _error_response("INVALID_ARGUMENT", 400)
            action = str(payload.get("action") or "").strip().lower()
            reason = str(payload.get("reason") or payload.get("note") or "").strip() or None
            res = await run_in_threadpool(
                lambda: tasks_store.set_task_content_rating(
                    task_id,
                    action=action,
                    reason=reason,
                    actor_user_id=actor,
                )
            )
        except TaskNotFound:
            return _error_response("TASK_NOT_FOUND", 404)
        except ContentRatingError as exc:
            return _error_response(exc.error_code, exc.status_code)
        except (ValueError, UnicodeDecodeError):
            return _error_response("INVALID_ARGUMENT", 400)
        return _json_response(res)

    async def handle_api_model_catalog(self, request: Request) -> JSONResponse:
        catalog = await run_in_threadpool(self.build_model_catalog)
        return _json_response(catalog)

    async def handle_api_mcp_status(self, request: Request) -> JSONResponse:
        status = await run_in_threadpool(self.build_mcp_status)
        return _json_response(status)

    async def handle_api_artifact_preview(self, request: Request) -> JSONResponse:
        artifact_id = request.path_params.get("artifact_id", "")
        include_nsfw = include_nsfw_from_headers(request.headers)
        tasks_store = self._active_tasks(request)
        artifacts_store = self._active_store(request)
        if not include_nsfw and tasks_store.is_artifact_nsfw(artifact_id):
            return _error_response("ARTIFACT_NOT_FOUND", 404)
        try:
            record = artifacts_store.get(artifact_id)
            path = artifacts_store.content_path(record)
        except (ValueError, ArtifactNotFound):
            return _error_response("ARTIFACT_NOT_FOUND", 404)
        except ArtifactError:
            return _error_response("ARTIFACT_UNAVAILABLE", 503)

        if record.media_type.startswith("audio/"):
            preview_mime, data_url = await run_in_threadpool(
                build_audio_preview_data_url, path, record.media_type, record.artifact_id
            )
            return _json_response(
                {
                    "artifact_id": record.artifact_id,
                    "filename": record.filename,
                    "media_type": record.media_type,
                    "preview_media_type": preview_mime,
                    "size": record.size,
                    "sha256": record.sha256,
                    "data_url": data_url,
                }
            )

        # Text / JSON / LRC preview
        raw = path.read_bytes()[:MAX_PREVIEW_RAW_BYTES]
        text_content = raw.decode("utf-8", errors="replace")
        return _json_response(
            {
                "artifact_id": record.artifact_id,
                "filename": record.filename,
                "media_type": record.media_type,
                "size": record.size,
                "sha256": record.sha256,
                "text": text_content,
            }
        )

    async def handle_api_artifact_chunk(self, request: Request) -> JSONResponse:
        artifact_id = request.path_params.get("artifact_id", "")
        chunk_str = request.path_params.get("chunk_index", "0")
        include_nsfw = include_nsfw_from_headers(request.headers)
        tasks_store = self._active_tasks(request)
        artifacts_store = self._active_store(request)
        if not include_nsfw and tasks_store.is_artifact_nsfw(artifact_id):
            return _error_response("ARTIFACT_NOT_FOUND", 404)
        try:
            chunk_index = int(chunk_str)
            if chunk_index < 0:
                raise ValueError
            record = artifacts_store.get(artifact_id)
            path = artifacts_store.content_path(record)
        except (ValueError, ArtifactNotFound):
            return _error_response("ARTIFACT_NOT_FOUND", 404)
        except ArtifactError:
            return _error_response("ARTIFACT_UNAVAILABLE", 503)

        total_size = record.size
        total_chunks = max(1, (total_size + CHUNK_RAW_BYTES - 1) // CHUNK_RAW_BYTES)
        if chunk_index >= total_chunks:
            return _error_response("CHUNK_OUT_OF_RANGE", 400)

        offset = chunk_index * CHUNK_RAW_BYTES
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(CHUNK_RAW_BYTES)

        return _json_response(
            {
                "artifact_id": record.artifact_id,
                "filename": record.filename,
                "media_type": record.media_type,
                "size": total_size,
                "sha256": record.sha256,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "data_b64": base64.b64encode(data).decode("ascii"),
            }
        )

    def routes(self) -> list[Route]:
        return [
            Route("/internal/dashboard-declaration", self.handle_internal, methods=["GET"]),
            Route("/music/internal/dashboard-declaration", self.handle_internal, methods=["GET"]),
            Route("/internal/dashboard-index", self.handle_internal, methods=["GET"]),
            Route("/music/internal/dashboard-index", self.handle_internal, methods=["GET"]),
            Route("/internal/dashboard/{asset:path}", self.handle_internal, methods=["GET"]),
            Route("/music/internal/dashboard/{asset:path}", self.handle_internal, methods=["GET"]),
            Route("/dashboard", self.handle_standalone_page, methods=["GET"]),
            Route("/music/dashboard", self.handle_standalone_page, methods=["GET"]),
            Route("/dashboard/api/mcp", self.handle_api_mcp_status, methods=["GET"]),
            Route("/music/dashboard/api/mcp", self.handle_api_mcp_status, methods=["GET"]),
            Route("/dashboard/{asset:path}", self.handle_standalone_page, methods=["GET"]),
            Route("/music/dashboard/{asset:path}", self.handle_standalone_page, methods=["GET"]),
            Route("/api/dashboard", self.handle_api_dashboard, methods=["GET"]),
            Route("/music/api/dashboard", self.handle_api_dashboard, methods=["GET"]),
            Route("/api/tasks", self.handle_api_list_tasks, methods=["GET"]),
            Route("/music/api/tasks", self.handle_api_list_tasks, methods=["GET"]),
            Route("/api/tasks/stats", self.handle_api_task_stats, methods=["GET"]),
            Route("/music/api/tasks/stats", self.handle_api_task_stats, methods=["GET"]),
            Route("/api/tasks/{task_id:str}", self.handle_api_task_detail, methods=["GET"]),
            Route("/music/api/tasks/{task_id:str}", self.handle_api_task_detail, methods=["GET"]),
            Route("/api/tasks/{task_id:str}", self.handle_api_task_delete, methods=["DELETE"]),
            Route("/music/api/tasks/{task_id:str}", self.handle_api_task_delete, methods=["DELETE"]),
            Route("/api/tasks/{task_id:str}/delete", self.handle_api_task_delete, methods=["POST"]),
            Route("/music/api/tasks/{task_id:str}/delete", self.handle_api_task_delete, methods=["POST"]),
            Route("/api/tasks/{task_id:str}/title", self.handle_api_task_title, methods=["POST"]),
            Route("/music/api/tasks/{task_id:str}/title", self.handle_api_task_title, methods=["POST"]),
            Route("/api/tasks/{task_id:str}/content-rating", self.handle_api_task_content_rating, methods=["POST"]),
            Route("/music/api/tasks/{task_id:str}/content-rating", self.handle_api_task_content_rating, methods=["POST"]),
            Route("/api/model-catalog", self.handle_api_model_catalog, methods=["GET"]),
            Route("/music/api/model-catalog", self.handle_api_model_catalog, methods=["GET"]),
            Route("/api/mcp", self.handle_api_mcp_status, methods=["GET"]),
            Route("/music/api/mcp", self.handle_api_mcp_status, methods=["GET"]),
            Route("/api/mcp/status", self.handle_api_mcp_status, methods=["GET"]),
            Route("/music/api/mcp/status", self.handle_api_mcp_status, methods=["GET"]),
            Route("/api/artifacts/{artifact_id:str}/preview", self.handle_api_artifact_preview, methods=["GET"]),
            Route("/music/api/artifacts/{artifact_id:str}/preview", self.handle_api_artifact_preview, methods=["GET"]),
            Route(
                "/api/artifacts/{artifact_id:str}/chunk/{chunk_index:str}",
                self.handle_api_artifact_chunk,
                methods=["GET"],
            ),
            Route(
                "/music/api/artifacts/{artifact_id:str}/chunk/{chunk_index:str}",
                self.handle_api_artifact_chunk,
                methods=["GET"],
            ),
        ]
