# music-minimax-h3-api

Local MiniMax H3 Base Ref2VA inference for the VerdantFlare Music Video workflow. The service uses SGLang's asynchronous OpenAI-compatible `/v1/videos` API directly; it does not add another proxy layer.

## Locked baseline

| Component | Version |
| --- | --- |
| MiniMax H3 | `42ed227ee7df40d41602854ae760620d6eb651fe` |
| SGLang | `0.5.18` |
| Base image | `lmsysorg/sglang:v0.5.18-cu130` |
| Service image | `music-minimax-h3-api-v0.1.0` |

Only the `Ref2VA` checkpoint partition is downloaded. Model weights and generated video remain outside the image and Git.

## Target hardware

The Chengdu validation deployment is pinned to `10.241.109.6`: eight distinct RTX 4090 GPUs, 512 GiB host memory, HAMI whole-card allocation, and local `hostpath` storage. RTX 4090 is not an upstream-verified H3 topology. The deployment therefore uses TP8 and layerwise CPU offload, and remains unverified until a real four-second Ref2VA request succeeds.

## API

- `GET /health`
- `POST /v1/videos`
- `GET /v1/videos/{id}`
- `GET /v1/videos/{id}/content`

Run the technical smoke test after the server becomes healthy:

```bash
H3_BASE_URL=http://127.0.0.1:8000 \
  services/music-minimax-h3-api/smoke-test.sh
```

The default reference is MiniMax's public technical sample. Passing this smoke test verifies the runtime contract only; it does not satisfy the Music Video acceptance requirement for approved appearance assets, approved music, or human visual review.

## License boundary

MiniMax H3 is not Apache or MIT licensed. Review `NOTICE.upstream.md` and the license downloaded with the model before providing access. The public product must implement the license's territory, user-terms, moderation, reporting, disclosure, and UI-attribution requirements before third-party use.
