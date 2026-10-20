# Upstream Notices

This image uses the following upstream projects without modifying their public service contracts:

- MiniMax Music 3, model revision `fbdf52fbaaca799592917417eb05f1899f1255ec`, under the MiniMax-Music3 Community License. See `LICENSE.minimax-music3`.
- SGLang-Omni `0.1.3`, under the Apache License 2.0.
- SGLang base image `lmsysorg/sglang:v0.5.16-cu130`, under the Apache License 2.0.
- UCX commit `d8e50df6651b9ea5b76f23aee0aefbf053a4137a`, under the BSD 3-Clause License.

The installed Python packages and base image retain their own license files and attribution notices.

The local runner adapter preserves the pinned upstream module, checks its SHA-256 at build time, and adds opt-in research trajectory capture and single-request prefix experiments. These do not add a public reference-audio or continuation API. The scheduler patch keeps CFG pairs atomic.
