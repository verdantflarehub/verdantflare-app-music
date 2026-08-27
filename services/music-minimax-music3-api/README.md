# music-minimax-music3-api

该目录构建 VerdantFlare Music 的 MiniMax Music 3 本地推理镜像。服务直接采用 SGLang-Omni 提供的 OpenAI 兼容语音接口，不在其外层重复实现代理 API。

镜像只提供供智能体和内部服务调用的 HTTP API，不包含 ComfyUI、Gradio 或其他 Web 操作界面。交互、试听和审核由 VerdantFlare Studio 承担。

## 锁定基线

| 组件                 | 版本                                               |
| -------------------- | -------------------------------------------------- |
| MiniMax Music 3      | `fbdf52fbaaca799592917417eb05f1899f1255ec`         |
| SGLang-Omni          | `0.1.3`                                            |
| SGLang CUDA 基础镜像 | `lmsysorg/sglang:v0.5.16-cu130`                    |
| UCX                  | `d8e50df6651b9ea5b76f23aee0aefbf053a4137a`         |
| 服务镜像             | `verdantflare-app:music-minimax-music3-api-v0.1.1` |

模型仓库中供当前 SGLang-Omni 稳定版使用的权重约 28.8 GB，不进入镜像或 Git。容器只下载该运行时实际读取的固定 revision 文件到挂载的模型目录；下载未完整完成时不会启动 API。

受限网络环境可设置标准变量 `HF_ENDPOINT` 切换 Hugging Face 下载 endpoint。`chengdu.beagle` 使用 `https://hf-mirror.com`，并设置 `HF_HUB_DISABLE_XET=1` 通过公开 HTTP/LFS 路径下载，避免 Xet CAS 跳转；不需要 Hugging Face 凭据。

## 资源要求

- 两张可见的 NVIDIA CUDA GPU。RTX 4090 部署必须给每个容器分配两张卡。
- NVIDIA Container Toolkit。
- 支持 CUDA 13.0 的 NVIDIA Linux 驱动，最低版本为 `580.65.06`。
- 至少 35 GB 可用模型存储。
- 32 GB 共享内存。容器必须使用 `--shm-size 32g` 和 `--ipc host`。

SGLang-Omni 将自回归阶段放在第一张可见 GPU，将 DIT/DAV 声学阶段放在第二张可见 GPU。启动入口发现少于两张 GPU 时会直接失败。
RTX 4090 验证环境为自回归阶段设置 `--mem-fraction-static 0.80`；默认值 `0.5` 不足以在加载 Qwen 权重后分配 KV cache。200 秒验收请求的 1,145-token 提示词需要为条件与无条件 CFG 两行预留至少 12,292 个 KV token；调低该值后必须重新核对启动日志中的 KV cache 容量。

## 构建

从仓库根目录执行：

```bash
docker build \
  -t verdantflare-app:music-minimax-music3-api-v0.1.1 \
  services/music-minimax-music3-api
```

## 运行

以下示例将宿主机的 GPU 0 和 1 分配给服务：

```bash
docker run --rm \
  --gpus '"device=0,1"' \
  --shm-size 32g \
  --ipc host \
  -p 8000:8000 \
  -v /data/models/MiniMax-Music3:/models/MiniMax-Music3 \
  verdantflare-app:music-minimax-music3-api-v0.1.1
```

目标验证环境使用 Kubernetes context `chengdu.beagle` 和 namespace `verdantflare-music`。仓库根目录的
[`deploy/chengdu.beagle/verdantflare-music/`](../../deploy/chengdu.beagle/verdantflare-music/)
提供声明式 Deployment、Service、模型 PVC 和双 GPU 探测 Job。部署前必须先运行探测 Job，并确认同一
Pod 内可见两个不同的 RTX 4090 UUID；不得在 HAMI GPU 份额上运行 Music3。

模型下载完成且两个推理阶段就绪后，健康检查返回 HTTP 200：

```bash
curl --fail http://127.0.0.1:8000/health
```

## 生成接口

服务公开 `POST /v1/audio/speech`。`input` 是带独立段落标签的歌词，`instructions` 是音乐描述；同一歌词、描述、seed 和长度产生确定性相同的输出。

```bash
curl --fail --show-error \
  http://127.0.0.1:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "MiniMaxAI/MiniMax-Music3",
    "input": "[Verse]\nMorning light across the open road\n[Chorus]\nWe carry on, we carry home",
    "instructions": "A warm acoustic pop song at 92 BPM with intimate vocals, fingerpicked guitar, soft piano, and a natural room sound.",
    "seed": 7,
    "max_new_tokens": 250,
    "response_format": "wav",
    "stream": false
  }' \
  --output /tmp/minimax-music3.wav
```

输出固定为 32 kHz、16-bit、双声道 WAV。`max_new_tokens` 表示 25 fps 的音频帧上限：`250` 约为 10 秒，`750` 约为 30 秒，最大 `9000` 约为 5 分钟。模型可能提前发出结束标记，因此该值是上限而不是保证时长。

段落标签必须单独占一行：

```text
[Verse]
Walking down the street
```

不得写成 `[Verse] Walking down the street`，否则同一行歌词会在标准化时被丢弃。

## 验收

服务启动后执行 10 秒真实推理冒烟测试：

```bash
services/music-minimax-music3-api/smoke-test.sh
```

脚本会检查健康状态，并验证输出为非空的 32 kHz、16-bit、双声道 WAV。输出默认写入 `/tmp/minimax-music3-smoke.wav`，不得提交到 Git。

## 许可证要求

MiniMax Music 3 使用自定义 Community License，不是 Apache 或 MIT 许可证。部署和产品接入前必须审阅 [LICENSE.minimax-music3](LICENSE.minimax-music3)，尤其注意：

- 商业产品或服务的用户界面必须显著展示 `MiniMax-Music3`。
- 相关产品或服务及其关联方年收入合计超过 2,000 万美元时，需要事先取得 MiniMax 书面授权。
- 对第三方提供生成能力时，必须建立并持续维护用于防止违法、侵权和违反可接受使用政策的技术及组织措施。

上游组件和版本说明见 [NOTICE.upstream.md](NOTICE.upstream.md)。
