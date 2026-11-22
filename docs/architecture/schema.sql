-- StreamRelay schema v1 (PostgreSQL 16). Managed by Alembic; this file is the reference.

CREATE TABLE users (
    id            uuid PRIMARY KEY,
    email         text UNIQUE NOT NULL,
    password_hash text NOT NULL,              -- argon2id
    created_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz
);

CREATE TABLE devices (
    id           uuid PRIMARY KEY,
    owner_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         text NOT NULL,
    platform     text NOT NULL CHECK (platform IN ('ios', 'android', 'test')),
    token_hash   bytea NOT NULL UNIQUE,       -- SHA-256 of the 256-bit device token
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    revoked_at   timestamptz,
    UNIQUE (id, owner_id)                     -- target for composite FKs: ties rows to one owner
);

CREATE TABLE pairing_codes (
    code_hash  bytea PRIMARY KEY,
    owner_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);

CREATE TABLE destinations (
    id               uuid PRIMARY KEY,
    owner_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind             text NOT NULL CHECK (kind IN ('twitch', 'youtube', 'rtmp')),
    name             text NOT NULL,
    ingest_url       text NOT NULL,
    stream_key_enc   bytea NOT NULL,          -- AES-256-GCM, key from server config, never returned by the API
    stream_key_nonce bytea NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id),
    CHECK (ingest_url LIKE 'rtmps://%')       -- encrypted ingest only (ADR 0006)
);

CREATE TYPE broadcast_state AS ENUM
    ('starting', 'live', 'holding', 'catching_up', 'finishing', 'finished', 'failed');

CREATE TABLE broadcasts (
    id                   uuid PRIMARY KEY,    -- UUIDv7
    owner_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id            uuid NOT NULL,
    client_ref           uuid NOT NULL,       -- idempotent creation
    title                text NOT NULL,
    state                broadcast_state NOT NULL DEFAULT 'starting',
    catch_up_mode        text NOT NULL DEFAULT 'catch_up' CHECK (catch_up_mode IN ('catch_up', 'hold', 'skip_to_live')),
    hold_max_delay_s     integer CHECK (hold_max_delay_s > 0),   -- 'hold' only: NULL = never speed up
    catch_up_max_speed   numeric(3,2) NOT NULL DEFAULT 1.25 CHECK (catch_up_max_speed BETWEEN 1.0 AND 1.5),
    buffer_target_ms     integer NOT NULL DEFAULT 8000,
    archive              boolean NOT NULL DEFAULT true,
    created_at           timestamptz NOT NULL DEFAULT now(),
    finish_requested_at  timestamptz,
    finished_at          timestamptz,
    deleted_at           timestamptz,
    UNIQUE (device_id, client_ref),
    UNIQUE (id, owner_id),
    -- the device must belong to the broadcast's owner, not merely exist
    FOREIGN KEY (device_id, owner_id) REFERENCES devices (id, owner_id)
);
CREATE INDEX broadcasts_owner ON broadcasts (owner_id, created_at DESC);

CREATE TABLE inits (
    broadcast_id uuid NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    init_id      integer NOT NULL,
    sha256       bytea NOT NULL,
    size_bytes   integer NOT NULL,
    storage_key  text NOT NULL,
    codec        text NOT NULL,               -- e.g. 'avc1.64001f,mp4a.40.2'
    width        integer, height integer, fps numeric(5,2),
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (broadcast_id, init_id)
);

CREATE TABLE epochs (
    broadcast_id   uuid NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    epoch          integer NOT NULL,
    first_start_us bigint,
    last_sequence  integer,                   -- set when the client ends the epoch
    end_reason     text,
    PRIMARY KEY (broadcast_id, epoch)
);

CREATE TABLE segments (
    broadcast_id uuid NOT NULL,
    epoch        integer NOT NULL,
    sequence     integer NOT NULL,
    init_id      integer NOT NULL,
    start_us     bigint NOT NULL,
    duration_us  integer NOT NULL,
    size_bytes   integer NOT NULL,
    sha256       bytea NOT NULL,
    storage_key  text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    played_at    timestamptz,                 -- set once, by the playout; never played twice
    play_result  text CHECK (play_result IN ('played', 'skipped', 'damaged')),
    PRIMARY KEY (broadcast_id, epoch, sequence),
    FOREIGN KEY (broadcast_id, epoch) REFERENCES epochs ON DELETE CASCADE,
    FOREIGN KEY (broadcast_id, init_id) REFERENCES inits
);
CREATE INDEX segments_unplayed ON segments (broadcast_id, epoch, sequence) WHERE played_at IS NULL;

CREATE TABLE playout_checkpoints (
    broadcast_id   uuid PRIMARY KEY REFERENCES broadcasts(id) ON DELETE CASCADE,
    state          broadcast_state NOT NULL,
    epoch          integer,
    sequence       integer,
    frame_offset   integer NOT NULL DEFAULT 0,
    speed          numeric(3,2) NOT NULL DEFAULT 1.0,
    output_frames  bigint NOT NULL DEFAULT 0,    -- output clock
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE broadcast_destinations (
    broadcast_id   uuid NOT NULL,
    destination_id uuid NOT NULL,
    owner_id       uuid NOT NULL,
    -- broadcast and destination must share one owner
    FOREIGN KEY (broadcast_id, owner_id)   REFERENCES broadcasts (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (destination_id, owner_id) REFERENCES destinations (id, owner_id),
    enabled        boolean NOT NULL DEFAULT true,
    status         text NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'connecting', 'up', 'retrying', 'failed', 'stopped')),
    last_error     text,
    reconnects     integer NOT NULL DEFAULT 0,
    PRIMARY KEY (broadcast_id, destination_id)
);

CREATE TABLE events (                          -- append-only timeline for the dashboard and debugging
    id           bigserial PRIMARY KEY,
    broadcast_id uuid NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    at           timestamptz NOT NULL DEFAULT now(),
    kind         text NOT NULL,                -- state_change, destination_up, destination_down, epoch_end, heartbeat_lost, ...
    detail       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_broadcast ON events (broadcast_id, at);

CREATE TABLE device_heartbeats (               -- latest only; history goes to events/metrics
    broadcast_id uuid PRIMARY KEY REFERENCES broadcasts(id) ON DELETE CASCADE,
    at           timestamptz NOT NULL,
    body         jsonb NOT NULL
);
