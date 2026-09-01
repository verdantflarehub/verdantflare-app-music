# music-minimax-h3-sglang-benchmark

Single-RTX-4090 MiniMax H3 Ref2VA benchmark using SGLang's official consumer
GPU execution path. This is an offline benchmark image, not a second product
API and not a ComfyUI installation.

## Locked runtime

| Component | Version |
| --- | --- |
| SGLang | `175973d8346e0309c293069c08364f5a4bc799dd` |
| Base CUDA image | `registry.cn-qingdao.aliyuncs.com/wod/verdantflare-app:music-minimax-h3-api-v0.1.1` |
| comfy-kitchen | `0.2.31` |
| FlashInfer Python/cubin | `0.6.18` |
| MiniMax H3 | `42ed227ee7df40d41602854ae760620d6eb651fe` |

The base image only supplies the CUDA environment and build dependencies
already validated on the target cluster. The Docker build checks out the exact
SGLang source revision before reinstalling its diffusion package; the old API
entrypoint and patched v0.5.18 Python code are not used at runtime.

## Fixed 4090 placement

Every run uses one GPU, TP1, Ulysses1, exact FlashAttention, eager execution,
zero resident DiT layers, and layerwise offload for only the DiT and text
encoder. The video VAE remains outside layerwise offload, matching the official
1344x768 RTX 4090 recipe.

`num_inference_steps` is recorded as sigma points. MiniMax H3 performs one
fewer DiT evaluation because the grid contains the terminal zero: 50 points is
49 NFE, 21 points is 20 NFE, and 5 points is 4 NFE.

## Output contract

Each case runs a seed-0 warmup followed by the timed seed-7 request. The output
directory contains the source reference and SHA-256, request and status JSON,
MP4 and ffprobe JSON, elapsed wall time, GPU samples, cgroup peak memory, and
the complete SGLang server log. Existing output directories are rejected so a
rerun cannot overwrite evidence.

The Kubernetes jobs and serial execution instructions live in
`deploys/k8s.cn-chengdu.bc-cloud.com/verdantflare-music/benchmarks/` in the
design repository.
