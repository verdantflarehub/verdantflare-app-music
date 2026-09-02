# Upstream notices

This benchmark packages the pinned MiniMax-H3-Turbo Diffusers inference scripts and runs the MiniMax H3 Ref2VA checkpoint with the LightX2V Ref2VA Turbo LoRA.

- SGLang CUDA base image: Apache License 2.0, version `0.5.18`, https://github.com/sgl-project/sglang
- Minimax-H3-Turbo: Apache License 2.0, commit `02e26d591f7a04d5d1a074c9566d5dd4f22f6225`, https://github.com/ModelTC/Minimax-H3-Turbo
- Diffusers: Apache License 2.0, version `0.40.0`, https://github.com/huggingface/diffusers
- LightX2V Ref2VA Turbo LoRA: model repository revision `05ef678438e84933c406131b59abbf86919b3aac`, https://huggingface.co/lightx2v/Minimax-h3-Turbo
- MiniMax H3: MiniMax H3 Community License Agreement, model revision `42ed227ee7df40d41602854ae760620d6eb651fe`, https://huggingface.co/MiniMaxAI/MiniMax-H3

The model and LoRA weights are mounted at runtime and do not enter the image or Git. MiniMax H3 use is territorially restricted and subject to its Acceptable Use Policy. Commercial user interfaces must prominently display `MiniMax H3`; generated public content must be clearly identified as AI-generated.
