# music-rvc-api

该目录构建 VerdantFlare Music 的 RVC v2 训练与音色转换服务。服务使用锁定的 RVC 上游实现，通过受控 HTTP API 向智能体和内部服务提供同步训练验收和音色转换，不包含 Gradio、RVC WebUI 或其他 Web 操作界面。

## 服务边界

该服务负责：

- 使用上传的真人录音训练不可覆盖的 RVC v2 voice model。
- 返回 `.pth`、`.index` 和基于输入前 15 秒执行真实转换的验证 WAV。
- 发现已训练或管理员预先安装的 RVC voice model。
- 使用上传的音频和受控转换参数执行单次音色转换。
- 返回 PCM 16-bit WAV。

该服务不负责：

- 接受任意宿主机文件路径。
- 通过 API 上传来源不明的 `.pth`、`.index` 模型。
- 音轨分离、去混响或 UVR5 推理。
- 在 HTTP 进程内建立训练队列、Task 或 Artifact 数据库。

训练接口同步执行，仅用于打通最小验收链。生产调用必须由 Station Runtime 创建 Task Attempt、分配 GPU 和登记项目资产，再调用该接口；客户端断开不等同于可靠的任务取消。

## 锁定基线

| 组件 | 版本 |
| --- | --- |
| RVC | `2.2.231006` |
| RVC commit | `9f2f0559e6932c10c48642d404e7d2e771d9db43` |
| PyTorch | `2.4.1` |
| CUDA | `12.4` |
| RVC runtime model revision | `e6d0c1a17da07c33557852f9dfa2bd44cc75737d` |
| 服务镜像 | `verdantflare-app:music-rvc-api-v0.1.1` |

上游代码和依赖进入镜像；模型与镜像分离。容器首次启动时下载固定 revision 的 HuBERT、RMVPE 和 RVC v2 40 kHz 预训练权重到持久化 runtime 目录，不下载 UVR5 模型。

模型下载默认使用 `https://huggingface.co`。受限网络环境可设置标准环境变量
`HF_ENDPOINT` 替换下载 endpoint；`chengdu.beagle` 验证环境使用 `https://hf-mirror.com`，
模型仓库、固定 revision 和目标文件列表保持不变。

## 模型目录

生产环境必须将持久化存储挂载到 `/models/rvc`：

```text
/models/rvc/
├── runtime/
│   ├── hubert_base.pt
│   ├── rmvpe.pt
│   └── pretrained_v2/
│       ├── f0G40k.pth
│       └── f0D40k.pth
└── voices/
    └── <model-id>/
        ├── model.pth
        └── model.index  # 可选
```

`model-id` 只能包含 1 至 64 个 ASCII 字母、数字、点、下划线或连字符。API 只接收模型 ID，并将其解析到上述固定目录；符号链接不得指向模型目录之外。

RVC checkpoint 通过 PyTorch 加载，可能包含可执行的 pickle 数据。只允许管理员安装来源可信并经过审核的模型，不能把用户上传的 checkpoint 直接放入模型目录。

### 训练音色

```bash
curl --fail --show-error \
  http://127.0.0.1:8000/v1/voice-models/train \
  -F audio=@User_Voice_10min.mp3 \
  -F model_id=TonyStark \
  -F epochs=200 \
  -F batch_size=4 \
  -F save_every_epochs=50 \
  --output TonyStark.zip
```

最大训练录音为 500 MiB。`model_id` 一经成功创建不可覆盖；重新训练必须使用新的模型 ID。ZIP 包含 `TonyStark.pth`、`TonyStark.index`、`TonyStark_validation.wav` 和 `manifest.json`，模型同时原子安装到 voice catalog。训练与转换共用同一 GPU 串行锁。

## 构建

从仓库根目录执行：

```bash
docker build \
  -t verdantflare-app:music-rvc-api-v0.1.1 \
  services/music-rvc-api
```

国内构建环境可以替换同版本基础镜像和 Python 软件源：

```bash
docker build \
  --build-arg BASE_IMAGE=registry.cn-qingdao.aliyuncs.com/wod/pytorch:2.4.1-cuda12.4-cudnn9-devel \
  --build-arg PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  -t verdantflare-app:music-rvc-api-v0.1.1 \
  services/music-rvc-api
```

## 运行

容器需要一张支持 CUDA 12.4 的 NVIDIA GPU。以下示例将宿主机 GPU 0 分配给服务：

```bash
docker run --rm \
  --gpus '"device=0"' \
  --shm-size 8g \
  -p 8000:8000 \
  -v /data/models/rvc:/models/rvc \
  verdantflare-app:music-rvc-api-v0.1.1
```

服务固定使用单个 Uvicorn worker，并在进程内串行执行训练和转换，避免 GPU 并发与模型切换冲突。容器没有可见 CUDA GPU、基础权重下载失败或 API 无法启动时会直接失败。

## API

### 健康检查

```bash
curl --fail http://127.0.0.1:8000/health
```

只有 CUDA、HuBERT 和 RMVPE 均可用时才返回 HTTP 200。响应包含 CUDA 状态、runtime 文件状态和已发现的 voice model 数量。

### 查询模型

```bash
curl --fail http://127.0.0.1:8000/v1/voice-models
```

响应只公开模型 ID 和是否存在索引，不返回服务器文件路径。

### 转换音色

```bash
curl --fail --show-error \
  http://127.0.0.1:8000/v1/audio/voice-conversions \
  -F audio=@/data/input/vocal.wav \
  -F model_id=approved-singer \
  -F pitch_shift=0 \
  -F f0_method=rmvpe \
  --output /tmp/converted.wav
```

接口使用 `multipart/form-data`，最大输入为 100 MiB。

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `audio` | 必填 | 可由 FFmpeg/AV 解码的音频 |
| `model_id` | 必填 | 已安装的受控模型 ID |
| `speaker_id` | `0` | 非负整数 |
| `pitch_shift` | `0` | `-24` 至 `24` 半音 |
| `f0_method` | `rmvpe` | `rmvpe`、`harvest`、`pm`、`crepe` |
| `index_rate` | `0.66` | `0.0` 至 `1.0`；无索引时自动设为 `0` |
| `filter_radius` | `3` | `0` 至 `7` |
| `resample_sr` | `0` | `0` 表示模型采样率，或 `16000` 至 `48000` |
| `rms_mix_rate` | `1.0` | `0.0` 至 `1.0` |
| `protect` | `0.33` | `0.0` 至 `0.5` |

模型不存在返回 HTTP 404，参数或空输入返回 4xx，转换执行失败返回 HTTP 500。服务不会在错误响应中暴露上游 traceback 或本地路径。

## 验收

本地静态测试：

```bash
PYTHONPATH=services/music-rvc-api \
  python3 -m unittest discover -s services/music-rvc-api/tests -v
```

目标 GPU 上安装一个批准的 voice model 并准备不含敏感信息的输入音频，然后执行真实转换：

```bash
MODEL_ID=approved-singer \
INPUT_FILE=/data/input/vocal.wav \
  services/music-rvc-api/smoke-test.sh
```

冒烟脚本会检查健康状态、模型发现、转换响应及 WAV 格式。只有镜像构建、GPU 启动和真实转换全部通过后，才能将该服务标记为已验证。
