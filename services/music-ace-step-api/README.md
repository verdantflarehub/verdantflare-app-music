# Music ACE-Step API

This image packages ACE-Step 1.5 from the pinned upstream commit in `Dockerfile`. It starts the upstream REST API with `acestep-v15-base` loaded and no LM. Model weights are downloaded at runtime into `/app/checkpoints`; mount a persistent volume there. The image contains no model weights or user audio.

The service is intended for the internal `verdantflare-music` network only. Inject `ACESTEP_API_KEY` from a Kubernetes Secret, and configure Music MCP's `MUSIC_EDITOR_URL` and `MUSIC_EDITOR_BEARER_TOKEN` to match. Do not expose the API through the public gateway.

Verify `/health`, then `/v1/models` with `acestep-v15-base` reporting `is_loaded=true` before calling `workflow.preflight(workflow="duet_generate")`. A successful model load still requires real short-sample tests for vocal separation, gender distinction, lyric placement, and musical quality.
