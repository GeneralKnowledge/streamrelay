# 0008: Segment storage

## Decision
A `SegmentStore` interface (`put`, `get`, `exists`, `delete`) with two backends: local filesystem (default, M1–M7) and S3-compatible (M8). An upload is acknowledged only after the file is fsynced (filesystem) or the object write has completed (S3). Buckets private; no public segment URLs; downloads go through the API with authorisation.

Self-hosters get filesystem by default. For S3-compatible self-hosting, document Garage or SeaweedFS; MinIO's community edition is in maintenance mode, so it is not the recommendation.

## Revisit if
Several servers need to share storage (then S3 becomes the default).
