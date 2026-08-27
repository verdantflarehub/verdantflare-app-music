# Music UVR5 API（草案）

API-only 的 GPU 音轨分离服务。`POST /v1/audio/stem-separations` 接收 multipart 音频，返回 ZIP：

- `instrumental.wav`：24-bit/48 kHz
- `vocal_dry_original.wav`：24-bit/48 kHz，经过独立去混响模型
- `manifest.json`

实现锁定 `audio-separator==0.46.0`，先使用 `mel_band_roformer_big_beta4.ckpt` 分离，再使用 `dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt` 去混响。两个 Roformer 模型均使用镜像中 CUDA 12.8 版本的 PyTorch；`audio-separator` 公共模块要求 ONNX Runtime，因此只安装 CPU 版 `onnxruntime==1.29.0`，不安装要求 CUDA 13 的 `onnxruntime-gpu`。模型由上游模型目录下载到 `/models/audio-separator` 挂载卷，不写入镜像。

```bash
docker run --rm --gpus all -p 8000:8000 \
  -v uvr-models:/models/audio-separator \
  verdantflare-app:music-uvr5-api-v0.1.2
```

当前仅通过静态和单元测试；分离质量、显存占用和处理时间必须在目标 GPU 用 `Demo_Selected.mp3` 实测后验收。

`audio-separator` 使用 MIT License；两个社区模型权重的许可与来源必须按 [`NOTICE.upstream.md`](NOTICE.upstream.md) 在生产发布前单独审查。
