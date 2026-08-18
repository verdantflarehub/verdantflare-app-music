# VerdantFlare App Music

VerdantFlare App Music is the Station-side application suite for AI-assisted
music production. It exposes versioned MCP tools and internal HTTP APIs for
music generation, stem separation, voice-model training, voice conversion,
mixing, and mastering.

The repository is in its initial design and scaffolding stage. It does not yet
provide a runnable production stack.

## Responsibilities

This repository owns the services that run on VerdantFlare Station:

| Service | Responsibility |
| --- | --- |
| `music-mcp-server` | Expose music tools to the Station MCP boundary, validate requests, and submit work to the internal services. |
| `music-generator-api` | Provide a stable internal API for candidate song generation while isolating the selected model runtime. |
| `uvr5-separator-api` | Provide an API for stem separation, dereverberation, model loading, and job status. |
| `rvc-engine-api` | Provide APIs for voice-model training, model management, and voice conversion. |
| `audio-mixer-api` | Provide APIs for mixing, mastering, loudness normalization, and delivery encoding. |

The repository does not own the Agent Skill or Studio user interface:

- The `verdantflare-music` Skill belongs in
  `verdantflare-skills/skills/verdantflare-music/`. It converts user intent
  into a production plan, fills parameters, applies review checkpoints, and
  selects MCP tools.
- VerdantFlare Studio presents the plan, confirmations, tasks, assets, reviews,
  and delivery state.
- Station Runtime owns GPU scheduling, task attempts, resource leases,
  project-scoped storage, and artifact registration.

The execution boundary is:

```text
User
  -> verdantflare-music Skill
  -> Studio MCP Bridge
  -> music-mcp-server
  -> generator / UVR5 / RVC / mixer APIs
  -> Station tasks and project assets
```

The Skill plans the work; the MCP server exposes controlled tools; the API
services perform the work. Workflow orchestration must not be duplicated in
the Skill and MCP server.

## Production Workflow

The target workflow contains three business stages and explicit human review
points:

1. Create and approve a structured song plan, then generate candidate demos.
2. Separate the selected demo and train or select an approved voice model.
3. Convert the vocal, mix and master the tracks, then approve the delivery
   package.

Planned MCP capabilities correspond to the service boundaries:

```text
music.generate
stems.separate
voice.train
voice.convert
mix.master
```

Tool names and schemas remain provisional until the contracts are reviewed and
versioned under `contracts/mcp/`.

## Repository Structure

The planned source tree is organized by independently deployable service:

```text
.
├── README.md
├── AGENTS.md
├── Makefile
├── compose.yaml
├── .env.example
├── services/
│   ├── music-mcp-server/
│   ├── music-generator-api/
│   ├── uvr5-separator-api/
│   ├── rvc-engine-api/
│   └── audio-mixer-api/
├── contracts/
│   ├── http/
│   └── mcp/
├── deploy/
│   ├── compose/
│   └── station/
├── tests/
│   └── e2e/
└── scripts/
```

Each Python service uses the same local layout:

```text
services/<service>/
├── Dockerfile
├── pyproject.toml
├── src/<python_package>/
└── tests/
```

Service dependencies, container builds, and unit tests remain local to their
service. Cross-service HTTP and MCP schemas live in `contracts/`. Shared Python
packages will only be introduced when real cross-service duplication requires
them.

UVR5 and RVC are first-class services built and released by this repository.
Maintained upstream implementations may be used as pinned algorithm
dependencies, but their public WebUI, CLI, or container interface is not the
VerdantFlare service contract.

## Runtime Data

Source control contains code, contracts, deployment configuration, and small
non-media test fixtures. It must not contain source recordings, generated
audio, voice models, model weights, caches, or delivery files.

Station provides project-scoped and model storage at runtime. A deployment may
mount storage with the following logical layout:

```text
/data/verdantflare/music/
├── projects/
├── voice-models/
├── model-cache/
└── temp/
```

Services receive project and asset resource handles from the Station boundary;
they must not accept unrestricted host filesystem paths. Generated files are
registered as task artifacts and asset versions rather than committed to Git.

## Development Rules

- Develop on `dev`; keep `main` as the stable branch.
- Build and publish VerdantFlare-owned images for all five services.
- Pin upstream source, Python dependencies, base images, and model revisions.
- Preserve upstream license and attribution notices.
- Keep secrets, credentials, media, model weights, and runtime data out of Git.
- Add startup commands only after the corresponding path is executable and
  tested.
- Start with one working end-to-end path before expanding the complete
  production workflow.

## Design Sources

- [AI music producer workflow](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/design/workflow/verdantflare_music.md)
- [Private deployment and MCP design](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/design/workflow/verdantflare_music/AI%E9%9F%B3%E4%B9%90%E5%88%B6%E4%BD%9C%E4%BA%BA_0.%E6%8A%80%E6%9C%AF%E6%96%B9%E6%A1%88.md)

The design documents describe the target product. Claims about model
availability, local inference support, GPU usage, latency, and output quality
must be validated in this repository before they are treated as implementation
facts.
