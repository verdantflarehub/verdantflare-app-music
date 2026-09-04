# MiniMax H3 LightX2V Ref2VA benchmark

该目录提供独立的 LightX2V Ref2VA Turbo 4 NFE 基准镜像，不是生产 API，也不会部署 ComfyUI、Gradio 或节点运行时。它用于与现有 SGLang Base Ref2VA 50 NFE 做同输入、同 seed、同输出规格的受控比较。

## 固定基线

| 组件 | 版本或参数 |
| --- | --- |
| MiniMax H3 | `42ed227ee7df40d41602854ae760620d6eb651fe` |
| Minimax-H3-Turbo scripts | `02e26d591f7a04d5d1a074c9566d5dd4f22f6225` |
| Diffusers | `0.40.0` |
| LightX2V LoRA repository | `05ef678438e84933c406131b59abbf86919b3aac` |
| Ref2VA LoRA | `minimax_h3_ref2v_turbo_4step_v0.1_bf16.safetensors` |
| LoRA SHA-256 | `9e642fc8749c74f8da5e2382877ab5c7aa37b9a73b7fd0d6d457bd1b3cb1ae99` |
| 推理 | 4 NFE，video/audio shift `12/3`，reference resize `match`，seed `7` |

现有 SGLang 0.5.18 锁定 Diffusers 0.37.0，而正式的 MiniMax H3 Diffusers pipeline 从 0.40.0 才提供。因此本基准保持独立镜像，不升级或修改当前 `music-minimax-h3-api`。

## 输入

模型、LoRA、批准的 Ref2VA 输入和输出均使用挂载目录。镜像不保存业务素材。

Diffusers 模型布局与当前 SGLang PVC 不同，不能共用同一个模型目录。准备命令只下载锁定 revision 的 `transformer_ref`、text encoder、VAE、audio VAE、processor、tokenizer 和 schedulers，不下载未使用的 FL2VA `transformer`：

```bash
docker run --rm \
  --entrypoint /usr/local/bin/prepare-lightx2v-models \
  -e HF_ENDPOINT=https://hf-mirror.com \
  -e HF_HUB_DISABLE_XET=1 \
  -v /data/models/MiniMax-H3-Diffusers:/models/MiniMax-H3-Diffusers \
  -v /data/models/MiniMax-H3-Turbo:/models/MiniMax-H3-Turbo \
  verdantflare-app:music-minimax-h3-lightx2v-benchmark-v0.1.1
```

准备脚本写入 revision marker 并校验 LoRA SHA-256。模型和 LoRA 仍位于持久化存储，不进入镜像或 Git。

只下载 Diffusers Ref2VA 所需组件仍约需 145 GB；运行前应确认模型目录至少有 160 GB 可用空间。首轮单卡自动 CPU offload 还需要充足主机内存，目标验证节点应保留当前 H3 级别的内存配额。

`ref2va.json` 使用上游 Minimax-H3-Turbo 的 jobs schema，每个 example 必须明确设置 `task` 为 `ref2va`，并提供非空 `references`。媒体路径相对于 JSON 文件解析。不得用伪造素材替代产品验收输入。

```json
{
  "examples": [
    {
      "task": "ref2va",
      "prompt": "使用已批准 reference tags 的冻结 Generation Unit 提示词",
      "duration": 5,
      "megapixels": 1.0,
      "aspect_ratio": "9:16",
      "references": [
        {"type": "video", "path": "approved-reference.mp4"}
      ]
    }
  ]
}
```

上面的名称只展示 schema，不是验收素材。

## 构建与运行

从 `verdantflare-app-music` 仓库根目录执行：

```bash
docker build \
  -t verdantflare-app:music-minimax-h3-lightx2v-benchmark-v0.1.1 \
  services/music-minimax-h3-lightx2v-benchmark
```

首轮使用单张完整 RTX 4090 和 Diffusers 自动 CPU offload。FSDP2 会关闭 CPU offload；当前两张 24 GB 卡尚无可运行证据，因此不把它加入首轮命令。

```bash
docker run --rm \
  --gpus '"device=0"' \
  --ipc host \
  -v /data/models/MiniMax-H3-Diffusers:/models/MiniMax-H3-Diffusers:ro \
  -v /data/models/MiniMax-H3-Turbo:/models/MiniMax-H3-Turbo:ro \
  -v /data/benchmarks/h3/input:/inputs:ro \
  -v /data/benchmarks/h3/output:/outputs \
  verdantflare-app:music-minimax-h3-lightx2v-benchmark-v0.1.1
```

默认入口严格校验模型索引、jobs schema 和 LoRA SHA-256。每次运行在 `/outputs/<UTC run id>/` 生成：

- `manifest.json`：固定版本、参数、输入哈希、耗时、退出码和输出数量；
- `gpu.csv`：逐秒 GPU UUID、显存和利用率；
- `resource-time.txt`：墙钟时间和进程 CPU/内存统计；
- `inference.log`：上游推理日志；
- `media.json`：首个输出的 ffprobe 结果；
- `generated/`：MP4 验收候选。

基准通过的最低条件是进程退出码为 0、生成至少一个 MP4、视频和音频流均可解析。画面一致性、运动、口型和音频质量仍需人工审核，不能由接口成功代替。
