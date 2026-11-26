# remiqora-runtime

Public runtime for a personal, non-commercial Remiqora studio on temporary RunPod GPUs:
the container image recipe (`docker/Dockerfile`) and the pod-side runtime (`pod/`: deadline guard,
agent, verified incremental backups, SSRF-safe ingestion). It contains **no secrets, no hostnames,
no account identifiers, no model weights and no music**. All of these are injected at runtime.

Upstream: [inikolax/remiqora](https://github.com/inikolax/remiqora) (MIT) pinned at
`7061167ba5f119d0720ca11a632fca74d17d1b67`. Model weights are downloaded at session start from their
upstream sources; YuE2, SheetSage2 and MuScriptor weights are CC BY-NC 4.0 (non-commercial).
Third-party components keep their own licenses (see the image review artifacts of each build).
