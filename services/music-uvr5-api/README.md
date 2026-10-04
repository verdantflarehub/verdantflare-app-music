# Music UVR5 API（草案）

API-only 的 GPU 音轨分离服务。`POST /v1/audio/stem-separations` 接收 multipart 音频，返回 ZIP：

- `instrumental.wav`：24-bit/48 kHz
- `vocal_dry_original.wav`：24-bit/48 kHz，经过独立去混响模型
- `vocal_wet_original.wav`：24-bit/48 kHz，分离后的原湿人声，供诊断
- `vocal_reverb_original.wav`：24-bit/48 kHz，去混响模型的残留，供诊断
- `vocal_lead_reference.wav`、`backing_vocals_unreviewed.wav`：仅在配置并验证额外的主唱/和声分离模型后返回，均须人工听审
- `manifest.json`

实现锁定 `audio-separator==0.46.0`，先使用 `melband_roformer_big_beta4.ckpt` 分离，再使用 `dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt` 去混响。湿人声和混响残留可能仍含原主唱，不是独立和声，不能直接叠加到换声主唱。两个 Roformer 模型均使用镜像中 CUDA 12.8 版本的 PyTorch；`audio-separator` 公共模块要求 ONNX Runtime，因此只安装 CPU 版 `onnxruntime==1.29.0`，不安装要求 CUDA 13 的 `onnxruntime-gpu`。

可选和声分离不自动下载未知权重。部署方须先验证适用于湿人声的两 stem 模型、许可与输出名称，在模型卷中提供权重及所需配置，再同时设置 `UVR5_BACKING_MODEL_FILENAME`、`UVR5_BACKING_LEAD_STEM`、`UVR5_BACKING_VOCAL_STEM` 和 `UVR5_BACKING_MODEL_SHA256`。启动脚本校验文件与哈希；未配置时维持四轨输出。配置后模型加载或输出不符合契约即报错，不回退成伪和声。`backing_vocals_unreviewed.wav` 仅供试听，不代表已经排除原主唱泄漏。

启动入口从 `hf-mirror.com/Eddycrack864/audio-separator-models` 的固定 revision `785c7f7ec9dc7e9b0d0eb22616cdf4d00778a5b5` 显式下载两份权重和对应 YAML，支持断点续传，并校验文件字节数和 SHA-256。该镜像仓库中的四个文件与上游 `model-configs` Release 文件字节数一致。权重写入 `/models/audio-separator` 挂载卷，不进入镜像；服务只在四个文件完整后启动。

```bash
docker run --rm --gpus all -p 8000:8000 \
  -v uvr-models:/models/audio-separator \
  verdantflare-app:music-uvr5-api-v0.1.4
```

当前仅通过静态和单元测试；分离质量、显存占用和处理时间必须在目标 GPU 用 `Demo_Selected.mp3` 实测后验收。

`audio-separator` 使用 MIT License；两个社区模型权重的许可与来源必须按 [`NOTICE.upstream.md`](NOTICE.upstream.md) 在生产发布前单独审查。
