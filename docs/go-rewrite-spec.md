# TorBox Media Center Go Rewrite Specification

## 1. Purpose

Build a new Go implementation of TorBox Media Center: a small service that exposes a user's cached TorBox video downloads to local media clients as either:

1. a read-only FUSE filesystem containing virtual media files; or
2. a directory tree of `.strm` files containing TorBox direct download URLs.

Primary target use case: Plex/Jellyfin/Emby/Infuse/VLC consuming TorBox-hosted media without downloading full files to local storage.

The Go rewrite should preserve the existing application's user-facing behavior, while replacing the Python/FUSE streaming hot path with a lower-CPU implementation.

## 2. Non-goals

The application must not:

- add, delete, repair, rename, renew, or manage TorBox downloads;
- provide WebDAV;
- transcode media;
- persist full media files locally;
- attempt to bypass TorBox limits or ToS;
- support arbitrary non-video files by default.

## 3. Runtime modes

### 3.1 `strm` mode

Generate physical `.strm` files under `MOUNT_PATH`.

Expected layout when `RAW_MODE=false`:

```text
<MOUNT_PATH>/
  movies/<Movie Root Folder>/<Movie Filename>.strm
  series/<Series Root Folder>/<Season Folder>/<Episode Filename>.strm
```

Expected layout when `RAW_MODE=true`:

```text
<MOUNT_PATH>/<original TorBox file directory>/<filename>.strm
```

Behavior:

- On startup, create/update `.strm` files for all known cached video downloads.
- Each `.strm` file contains the TorBox request-download URL for that file.
- Periodically refresh TorBox downloads and update `.strm` files.
- Remove stale `.strm` files whose backing TorBox downloads disappeared.
- Remove empty directories left behind by stale files.

### 3.2 `fuse` mode

Mount a read-only FUSE filesystem at `MOUNT_PATH`.

Expected layout when `RAW_MODE=false`:

```text
/
  movies/
    <Movie Root Folder>/
      <Movie Filename>
  series/
    <Series Root Folder>/
      <Season Folder>/
        <Episode Filename>
```

Expected layout when `RAW_MODE=true`:

```text
/<original TorBox path>
```

Behavior:

- Files appear as regular read-only files with correct file sizes.
- Directories are read-only.
- Writes, renames, deletes, mkdir, truncation, chmod-like mutation should fail.
- Reads are served by HTTP range requests against TorBox direct download URLs.
- The mount must work with Plex-style random reads, startup probes, tail probes, and rapid seeks.

## 4. Configuration

Support environment variables compatible with the current app.

Required:

- `TORBOX_API_KEY`: TorBox API token.

Core:

- `MOUNT_METHOD`: `strm` or `fuse`. Default: `strm`.
- `MOUNT_PATH`: local path. Default may be `/torbox` in Docker, `./torbox` locally.
- `MOUNT_REFRESH_TIME`: one of:
  - `slowest` = 24h
  - `very_slow` = 12h
  - `slow` = 6h
  - `normal` = 3h
  - `fast` = 2h
  - `ultra_fast` = 1h
  - `instant` = 0.1h / 6 minutes, only meaningful with metadata enabled in the old app; may be supported generally if rate limits are respected.
- `ENABLE_METADATA`: boolean. Default: `false`.
- `RAW_MODE`: boolean. Default: `false`. If true, disable metadata scanning and preserve original TorBox paths.

Metadata tuning:

- `METADATA_MAX_WORKERS`: default `2` when metadata scanning is enabled.
- `METADATA_SEARCH_MIN_INTERVAL`: default `0.75s`; global throttle between metadata search request starts.

FUSE / streaming tuning, keep backwards-compatible names:

- `FUSE_FOREGROUND_SEGMENT_KB`: current tested value `2048`; default can be `1024` or `2048`.
  - In the Go design, treat this as read alignment/granularity, not as a separate foreground HTTP fetch size.
- `FUSE_PREFETCH_WINDOW_MB`: current tested value `16`; default may remain `8` for conservative upstream compatibility or `16` for Plex performance.
  - In the Go design, treat this as stream window size.
- `FUSE_PREFETCH_WAIT_MS`: compatibility knob. Current successful Plex tests used `0`.
- `FUSE_PREFETCH_MIN_AGE_MS`: compatibility knob. Less relevant with unified streaming windows.

Recommended new aliases, while preserving old envs:

- `STREAM_WINDOW_MB` alias of `FUSE_PREFETCH_WINDOW_MB`.
- `READ_ALIGNMENT_KB` alias of `FUSE_FOREGROUND_SEGMENT_KB`.
- `STREAM_CHUNK_MB` default `1`.
- `STREAM_CACHE_MB` default `512`.
- `STREAM_MAX_INFLIGHT_PER_PATH` default `2`.

## 5. TorBox API integration

Base APIs:

- TorBox API: `https://api.torbox.app/v1/api`
- TorBox Search API: `https://search-api.torbox.app`

Headers:

```http
Authorization: Bearer <TORBOX_API_KEY>
```

### 5.1 Download types

Fetch all three download categories:

- `torrents`
- `usenet`
- `webdl`

For each category, call:

```text
GET /<type>/mylist?limit=1000&offset=<offset>&bypass_cache=true
```

Pagination:

- Start offset at `0`.
- Increment by `limit` while returned data length equals limit.
- Stop on empty page or page smaller than limit.

Only include items where item field `cached` is truthy.

### 5.2 File filtering

Only expose video files with acceptable MIME types.

Current accepted types:

- `video/x-matroska`
- `video/mp4`

The Go implementation may optionally support more video MIME types behind a config flag, but default behavior should match current app.

### 5.3 Download URL construction

For each file, construct request-download URL:

```text
https://api.torbox.app/v1/api/<type>/requestdl?token=<TORBOX_API_KEY>&<id_param>=<item_id>&file_id=<file_id>&redirect=true
```

`id_param` mapping:

- `torrents` -> `torrent_id`
- `usenet` -> `usenet_id`
- `webdl` -> `web_id`

In FUSE mode, resolve/cache the redirect target before streaming:

- `GET requestdl URL`
- if response is redirect, use `Location` as the direct download URL;
- cache direct URL per file/path for `LINK_AGE = 3h`;
- refresh direct URL when expired.

In `.strm` mode, writing the request-download URL is acceptable and matches existing behavior.

## 6. Media metadata and naming

The application creates an internal `MediaFile` model for every exposed file.

Required fields:

```go
type MediaFile struct {
    ItemID string
    SourceType string // torrents, usenet, webdl
    FolderName string
    FolderHash string
    FileID string
    OriginalFileName string
    FileSize int64
    MimeType string
    OriginalPath string
    DownloadURL string
    Extension string

    MediaType string // movie, series, anime
    RootFolderName string
    SeasonFolderName string
    DisplayFileName string
    Season *int
    Episode *int
    MetadataTitle string
    MetadataYear *int
    MetadataLink string
    MetadataImage string
    MetadataBackdrop string
}
```

### 6.1 Filename parsing

Parse media filename to extract at least:

- title;
- year;
- season;
- episode.

Python used `PTN.parse`. The Go rewrite can use an existing parser library or implement enough parsing for common names:

- `Title.S04E12...mkv`
- `Title - S04E12 - Episode Name.mkv`
- `Title (2023).mp4`

If parsing fails, fall back safely to original file name and item name.

### 6.2 Manual media type tags

Support manual routing tags from both item and file objects.

Inspect fields:

- `tags`
- `tag`
- `labels`
- `label`

Accept nested string/list/map forms. Normalize to lowercase strings. Recognize:

- `type=movie`
- `type=series`
- `type=anime`

Manual media type overrides metadata routing even when metadata scanning is disabled.

Routing:

- `movie` -> `/movies`
- `series` -> `/series`
- `anime` -> `/series`

For manual `series` / `anime`:

- root folder = TorBox item name;
- season folder = `Season <n>`, default season `1` if unknown;
- display filename = original file name.

### 6.3 Metadata disabled behavior

When `ENABLE_METADATA=false` and no manual type tag exists:

- media type defaults to `movie`;
- root folder name is TorBox item name;
- display filename is original file name.

### 6.4 Metadata enabled behavior

When `ENABLE_METADATA=true` and `RAW_MODE=false`:

Search metadata endpoint:

```text
GET /meta/search/<full_title>?type=file
```

Where `full_title` should combine item name and file name similarly to the current app.

Rate limit behavior:

- cap concurrency with `METADATA_MAX_WORKERS`;
- throttle search starts globally by `METADATA_SEARCH_MIN_INTERVAL`;
- handle HTTP 429 by respecting `Retry-After` when present, else exponential backoff with jitter;
- avoid synchronized retry storms.

On successful metadata result:

- if result type is `series` or `anime`:
  - media type = returned type;
  - root folder = `<clean title> (<year>)` when year exists;
  - season folder = `Season <season>`;
  - display filename = `<clean title> SxxExx<extension>` when season/episode are known.
- if result type is `movie`:
  - media type = `movie`;
  - root folder = `<clean title> (<year>)` when year exists;
  - display filename = `<clean title> (<year>)<extension>`.
- if unsupported/empty/error:
  - fall back to base metadata.

Sanitize path components by removing invalid filename characters:

```text
/ \ : * ? " < > |
```

Normalize year values like `2023-2024` to first year `2023`.

### 6.5 RAW_MODE behavior

If `RAW_MODE=true`:

- disable metadata scanning regardless of `ENABLE_METADATA`;
- preserve original TorBox file path from file field `name`;
- do not create `/movies` and `/series` roots;
- expose the original directory tree.

## 7. Local state

The current Python app uses TinyDB JSON files per download type. The Go rewrite can choose a better store.

Required behavior:

- Keep a durable local catalog of last fetched media files.
- On startup, fetch fresh TorBox data before serving when possible.
- If TorBox fetch fails but a previous catalog exists, optionally serve stale catalog with warning rather than empty mount.
- Refresh catalog periodically according to `MOUNT_REFRESH_TIME`.
- Updates must be atomic from FUSE perspective: readers should see either old catalog or new catalog, never partial state.

Recommended implementation:

- Store catalog as a single JSON file under an app data directory, or SQLite/BoltDB if preferred.
- Use immutable snapshots for FUSE VFS: build new tree, then atomically swap pointer.

## 8. FUSE filesystem contract

Recommended Go libraries:

- `bazil.org/fuse` / `bazil.org/fuse/fs`, or
- `github.com/hanwen/go-fuse/v2`.

The FUSE mount must provide:

- `Lookup`
- `ReadDirAll` / equivalent
- `Attr`
- `Open`
- `Read`
- `Release`

Read-only semantics:

- directories mode `0755`;
- files mode `0444`;
- all mutation operations return permission/read-only errors.

File attributes:

- correct file size from TorBox file size;
- stable inode IDs if feasible;
- reasonable atime/mtime/ctime.

Startup behavior:

- Initialize empty structures first.
- Fetch/build catalog synchronously once before mounting or before reporting ready.
- Then start periodic refresh in background.
- Avoid the old race where background refresh could build VFS and constructor later overwrote it with empty state.

## 9. FUSE streaming design

This is the most important part of the rewrite.

### 9.1 Core model

Do not implement separate blocking foreground fetch plus background prefetch.

Use one unified streaming window model:

```text
read miss -> start stream window -> current read waits only for needed bytes -> remaining bytes continue filling window -> later reads join same inflight window or hit cache
```

Definitions:

- `read alignment`: currently compatible with `FUSE_FOREGROUND_SEGMENT_KB`; aligns read misses.
- `stream window`: currently compatible with `FUSE_PREFETCH_WINDOW_MB`; range size to fetch from CDN.
- `block size`: 64MB logical boundary; stream windows should not cross this boundary unless intentionally changed.
- `inflight stream`: active HTTP range stream filling a buffer.
- `segment cache`: byte-limited LRU cache of completed stream windows/ranges.

### 9.2 Successful tested settings

The current Python branch performed well subjectively with:

```env
FUSE_FOREGROUND_SEGMENT_KB=2048
FUSE_PREFETCH_WINDOW_MB=16
FUSE_PREFETCH_WAIT_MS=0
```

Observed behavior in testing:

- first bytes returned before full 16MB range completed;
- rapid Plex seeks generated only a few real FUSE miss targets due to Plex debounce/coalescing;
- first-join latency after real seeks was often ~0.3-0.9s;
- delayed next-window reduced wasted read-ahead during scrub sequences.

### 9.3 Read algorithm

On FUSE `Read(path, offset, size)`:

1. Clamp size to file boundary.
2. Resolve cached direct download URL for path, refreshing if older than 3h.
3. For each portion needed:
   - look for a completed cached range covering `[offset, offset+size)`;
   - if present, copy directly from cache into response;
   - else look for an inflight stream covering the needed bytes;
   - if present, wait until the needed bytes are available, not until whole stream finishes;
   - else start a new stream window at aligned `segment_start` with size `min(stream_window, block_end-segment_start+1)`;
   - wait for needed bytes from that stream;
   - update stream `max_served_offset`;
   - maybe start next stream window according to policy.
4. Return exactly requested bytes or FUSE I/O error if unrecoverable.

### 9.4 Inflight stream behavior

Each stream has:

```go
type StreamWindow struct {
    Path string
    Start int64
    End int64
    Buffer []byte
    Done bool
    Cancelled bool
    Err error
    StartedAt time.Time
    MaxServedOffset int64
    Cond *sync.Cond
    Cancel context.CancelFunc
}
```

HTTP request:

```http
Range: bytes=<start>-<end>
```

Stream in chunks, recommended chunk size: `1MB`.

On every chunk:

- append to stream buffer;
- signal waiters;
- stop promptly if context cancelled.

When complete:

- store completed data into LRU range cache if not cancelled;
- mark done;
- signal waiters;
- remove from inflight map.

### 9.5 Cache design

Avoid Python-style linear scans if possible.

Requirements:

- byte-limited LRU, default `512MB`;
- entries keyed by path and start offset;
- support efficient covering-range lookup: find range where `entry.Start <= offset && offset+size-1 <= entry.End`;
- do not store duplicate ranges covered by a larger existing range;
- evict least recently used by total byte size.

Recommended implementation:

- per-path interval index or ordered map by start offset;
- global LRU list for eviction;
- map key `(path,start)` to entry;
- update LRU on cache hit.

### 9.6 Read-ahead / next-window policy

Avoid aggressive speculative prefetch that wastes TorBox/CDN bandwidth.

Policy that tested well:

- max inflight streams per path: `2`;
- next-window starts only after playback has consumed enough bytes from current stream;
- threshold: `min(4MB, max(stream_window/2, read_alignment))` from current stream;
- do not start next-window merely because of arbitrary cache hits;
- do not start overlapping windows already covered by cache or inflight stream;
- deduplicate repeated read-ahead scheduling for the same start offset.

There are two reasons to start streams:

- `miss`: current read needs bytes not cached/inflight;
- `next-window` / `read-ahead`: sequential playback has advanced enough to justify next range.

### 9.7 Seek cancellation

Rapid Plex scrubbing can leave stale inflight streams. Cancel them conservatively.

Policy:

- On a substantial miss, check active streams for the same path.
- Do not cancel tiny tail/metadata probes.
- Define `seek_cancel_gap = max(stream_window*2, 64MB)`.
- If new miss offset is not within `seek_cancel_gap` of any active stream, treat as far seek and cancel stale streams for that path.
- Cancel via context; stream loop should stop quickly.
- Do not cache partial cancelled stream data by default.

### 9.8 Tail probes and small ranges

Plex may read near the end of the file for metadata/index information.

Requirements:

- Correctly handle short final windows.
- If range near EOF is smaller than stream window, fetch only remaining bytes.
- Tiny tail/metadata probes must not cancel active playback streams.

### 9.9 Error handling

- HTTP 200 and 206 are acceptable for range requests, but 206 is expected.
- On HTTP/range error, return read error and log concise warning.
- Retry transient network errors carefully; avoid multiplying Plex reads into retry storms.
- Respect context cancellation.

## 10. HTTP client behavior

Use a shared HTTP client with:

- sensible connect/read timeouts;
- connection pooling;
- redirect handling where appropriate;
- no caching for media range requests;
- small in-memory cache for non-media GETs only if needed.

429 handling:

- for metadata/search/list APIs: use `Retry-After` + jittered exponential backoff;
- for media range requests: be conservative; a few retries may help, but avoid high fanout.

## 11. Scheduler and lifecycle

Startup sequence:

1. Parse config and validate.
2. Initialize logging.
3. Ensure mount path exists.
4. If `strm` and `RAW_MODE=false`, ensure `movies` and `series` directories exist.
5. Fetch initial TorBox catalog.
6. Build media tree/catalog snapshot.
7. Start refresh scheduler.
8. Start selected mount mode.

Refresh:

- periodically fetch fresh TorBox catalog;
- rebuild media tree/catalog snapshot;
- in `strm` mode, update files and remove stale files;
- in `fuse` mode, atomically swap VFS snapshot.

Shutdown:

- unmount FUSE cleanly;
- cancel active stream contexts;
- close local store;
- in `strm` mode, existing app deletes generated files on process exit; the rewrite may either preserve current behavior or make it configurable. Preserve by default if strict compatibility is required.

## 12. Logging and observability

Default logs should be concise.

Info-level:

- config summary, but never print full API token;
- catalog refresh start/end counts;
- mount mode and mount path;
- FUSE mount/unmount;
- warning when serving stale catalog.

Warning/error:

- TorBox API failures;
- metadata failures after retry;
- FUSE mount errors;
- media range stream failures.

Do not keep per-read trace logs in normal builds.

Optional performance counters behind env flag, e.g. `FUSE_PERF_LOG=1`:

Every 10-30s log:

- reads/sec;
- bytes/sec;
- cache hit/miss counts;
- inflight joins;
- stream starts by reason;
- cancellations;
- cache size bytes/entries;
- inflight count;
- average/p95 read latency.

This is important for validating CPU improvements without verbose trace spam.

## 13. Docker requirements

Provide Docker image suitable for both modes.

For `strm`:

```yaml
volumes:
  - /host/torbox:/torbox
environment:
  - TORBOX_API_KEY=...
  - MOUNT_METHOD=strm
  - MOUNT_PATH=/torbox
```

For `fuse`:

```yaml
volumes:
  - /host/torbox:/torbox
devices:
  - /dev/fuse:/dev/fuse
cap_add:
  - SYS_ADMIN
security_opt:
  - apparmor:unconfined
environment:
  - TORBOX_API_KEY=...
  - MOUNT_METHOD=fuse
  - MOUNT_PATH=/torbox
```

Use a small runtime image if possible. Ensure CA certificates are present.

## 14. Compatibility tests

Implement tests at these levels.

### 14.1 Unit tests

- config parsing and defaults;
- refresh interval mapping;
- manual tag parsing from strings/lists/maps;
- filename parsing fallback;
- metadata title/year sanitization;
- VFS tree construction for raw and non-raw modes;
- `.strm` path generation;
- range cache covering lookup and LRU eviction;
- stream window alignment and block boundary calculations;
- seek cancellation decision logic.

### 14.2 Integration tests with fake TorBox server

Fake endpoints:

- `/torrents/mylist`, `/usenet/mylist`, `/webdl/mylist`;
- `/.../requestdl?...redirect=true` returning redirect location;
- direct media URL supporting HTTP Range.

Scenarios:

- startup with movies and series;
- metadata disabled;
- manual `type=series` tags;
- raw mode;
- stale download removal in `.strm` mode;
- FUSE read from offset 0;
- FUSE tail probe;
- rapid seek: start stream, then far offset read cancels stale stream;
- sequential reads start next-window only after threshold.

### 14.3 Manual Plex validation

Use environment:

```env
FUSE_FOREGROUND_SEGMENT_KB=2048
FUSE_PREFETCH_WINDOW_MB=16
FUSE_PREFETCH_WAIT_MS=0
```

Validate:

- initial playback starts promptly;
- rapid seek sequence resumes quickly after final seek;
- no excessive speculative bandwidth during scrubbing;
- CPU is materially lower than Python implementation during steady playback;
- no 429 storms.

## 15. Performance goals

Functional correctness is required first, but the reason for Go rewrite is CPU reduction.

Targets:

- steady Plex playback should use significantly less CPU than Python implementation under same file/player/network conditions;
- avoid per-read linear scans over large cache;
- minimize buffer copies;
- support serving many small Plex reads from cached/inflight windows efficiently;
- cancellation should stop network read promptly.

Implementation hints:

- Prefer `io.Reader` streaming into reusable buffers where practical.
- Keep completed cache entries immutable.
- Return slices/copies according to FUSE library requirements; avoid extra copies beyond required boundary.
- Use per-path locks or sharded locks rather than one global lock if contention appears.
- Use `context.Context` for stream cancellation.

## 16. Current known-good branch as behavioral reference

Reference implementation branch in fork:

```text
iiieii-miron/torbox-media-center:feature/fuse-streaming-pipeline
```

Important behavior from this branch:

- unified streaming window pipeline;
- range cache with covering-range lookup;
- manual `type=movie|series|anime` tags;
- metadata search throttling;
- delayed next-window read-ahead;
- stale stream cancellation on far seek;
- temporary trace logs removed;
- minor CPU-churn reduction via 1MB stream chunks and read-ahead dedup.

Do not blindly port Python structure; port the behavior and improve implementation with Go-native data structures.

## 17. Suggested module structure

```text
cmd/torbox-media-center/main.go
internal/config/
internal/torbox/
internal/catalog/
internal/metadata/
internal/naming/
internal/strm/
internal/fusefs/
internal/streaming/
internal/rangecache/
internal/scheduler/
internal/logging/
```

Key boundaries:

- `torbox`: API client and DTOs.
- `catalog`: media file model, refresh, persistence.
- `naming`: metadata/manual tags/path generation.
- `strm`: `.strm` materialization and stale cleanup.
- `fusefs`: FUSE node operations.
- `streaming`: direct URL cache, stream windows, read algorithm.
- `rangecache`: byte-limited range LRU.

## 18. Acceptance criteria

A first production candidate is acceptable when:

1. Docker image runs in both `strm` and `fuse` mode.
2. Existing env vars are accepted.
3. TorBox cached video downloads appear under expected paths.
4. Manual type tags route series/anime correctly with metadata disabled.
5. RAW_MODE preserves original paths.
6. FUSE files are read-only and report correct sizes.
7. Plex can start playback and seek through large MKV/MP4 files.
8. Rapid seek does not keep downloading many stale windows.
9. CPU during steady playback is lower than the Python FUSE implementation in comparable conditions.
10. Logs are clean at info level, with optional perf counters available.
