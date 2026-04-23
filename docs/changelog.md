# Changelog

All notable changes to HyperHTTP will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] - 2026-04-19

### Security
- **Strip credentialed headers on unsafe redirects.** `Authorization`,
  `Proxy-Authorization`, and `Cookie` are now removed whenever a redirect
  crosses origins (scheme/host/port) or downgrades from `https://` to
  `http://`. Previously, a compromised or malicious upstream could capture
  tokens by returning a `Location:` pointing at an attacker domain. Auth
  helpers re-apply their own headers on every attempt, so same-origin
  chains still work transparently. (C1)
- **Reject CRLF / NUL / non-token characters in request construction.**
  Header names must now be valid RFC 7230 tokens; header values, request
  methods, request targets, URL hostnames, multipart field names, and
  multipart filenames are rejected if they contain `\r`, `\n`, or `\0`.
  Previously these were written verbatim to the wire, enabling HTTP
  request-smuggling, header injection, and multipart-part smuggling. (C2)
- **Cap decompressed response size.** New `max_decompressed_size`
  `Client` knob (default 64 MiB) applied inside every decoder
  (gzip/deflate/brotli/zstd). Brotli input is also bounded pre-decode
  because the C decoder cannot stream, which previously made a ~1 KB
  brotli payload inflatable to unlimited memory. Raises
  `hyperhttp.DecompressionError`. (H1)
- **Consumer-driven HTTP/2 flow control.** Per-stream DATA queues are
  now bounded (32 chunks) and `acknowledge_received_data` fires only
  after the consumer drains each chunk. Previously the reader pre-acked
  every DATA frame, letting a hostile server stream an unbounded body
  regardless of the advertised window. (H2)
- **Cap response body size.** New `max_response_size` `Client` knob
  enforces a maximum raw-body byte count across both the streaming
  (`aiter_bytes`, `aiter_raw`) and materialising (`aread`) paths. An
  oversize `Content-Length` is rejected before any bytes are read.
  Raises `hyperhttp.ResponseTooLarge`. (H3)
- **Fix pool waiter cancellation race.** A transport handed off to a
  waiter whose `acquire()` had just timed out is now reclaimed back to
  the pool. Previously the connection could be stranded in the active
  set, slowly exhausting `max_connections`. (M1)
- **Never leak `Proxy-Authorization` to origin servers.** The H1
  transport now strips any user-supplied `Proxy-Authorization` header
  before writing the request head; on absolute-form (plain HTTP via
  HTTP proxy) the value from the proxy URL always wins. H2 already
  filtered this hop-by-hop header. (M2)
- **Warn on `verify=False`.** Disabling TLS verification now emits
  `hyperhttp.InsecureRequestWarning` at `Client` construction and
  on every `create_ssl_context` call, so misconfiguration surfaces
  in tests/CI instead of shipping silently. (M3)
- **Scrub URLs in retry logs; validate redirect references.** The
  retry handler now logs `URL.sanitized()` (no query string, no
  userinfo), so retry loops don't exfiltrate `?api_key=` or
  `user:pass@` credentials. `URL.join()` rejects CR/LF/NUL/TAB in
  the redirect reference. New `URL.sanitized()` helper.

### Added
- `hyperhttp.InsecureRequestWarning` (exported from top-level).
- `hyperhttp.ResponseTooLarge`, `hyperhttp.DecompressionError`
  exceptions (exported).
- `hyperhttp.LocalProtocolError` is now exported from the top level.
- `Client(max_response_size=..., max_decompressed_size=...)` knobs.
- `URL.sanitized()` returns a log-safe string with query and userinfo
  redacted.

### Changed
- HTTP/1 header insertion (`Headers.add`, `Headers.set`, bulk update,
  constructor) now validates names and values. This replaces silent
  corruption with an explicit `LocalProtocolError` for anything that
  would corrupt the wire framing.
- `Client` now emits `InsecureRequestWarning` at construction when
  `verify=False` is passed (and no `ssl_context` override is provided).

- `MockTransport` / `MockResponse` / `Router`: in-memory transport for
  tests. Accepts a callable handler (sync or async), a replay sequence,
  a single response, or a route mapping. Pass as
  `Client(transport=MockTransport(...))` — the entire production stack
  (retries, auth, event hooks, cookies, redirects) runs, only the socket
  is replaced. Recorded calls are exposed via `mock.calls`, `mock.call_count`,
  `mock.last_request`, and `mock.reset()`. Handlers can raise any
  `hyperhttp` exception to exercise failure paths.
- Event hooks: `Client(event_hooks={"request": [...], "response": [...]})`.
  Hooks are sync or async callables that fire once per network attempt,
  unlocking OpenTelemetry / distributed tracing, structured logging,
  request signing (AWS SigV4, OAuth1), and per-request metrics. The
  `event_hooks` dict is also writable on a live client.
- Authentication helpers: `hyperhttp.BasicAuth`, `hyperhttp.BearerAuth`,
  `hyperhttp.DigestAuth`, and the extensible `hyperhttp.Auth` base class.
  Configure with `Client(auth=...)` or per-request `client.get(..., auth=...)`.
  `auth=("user", "pass")` is shorthand for `BasicAuth`; `auth=None` on a
  request disables a client-level default. `DigestAuth` handles the
  `401 → WWW-Authenticate → retry` round-trip and supports MD5, SHA-256,
  SHA-512-256, their `-sess` variants, and `qop=auth`.
- HTTP and HTTPS proxy support via the new `proxies=` and `trust_env=` client
  arguments. HTTPS targets are tunnelled via `CONNECT`; environment variables
  (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`) are honoured by
  default. New `hyperhttp.ProxyURL` helper and `hyperhttp.ProxyError` exception.
- `multipart/form-data` uploads via `Client.post(files=..., data=...)`. File
  parts stream directly from disk in 1 MiB chunks with a pre-computed
  `Content-Length` (no chunked framing overhead), so a 10 GiB upload uses
  O(chunk) memory. New `hyperhttp.MultipartEncoder` and
  `hyperhttp.MultipartFile` for advanced control. Local benchmark
  (`examples/benchmark_multipart.py`) measures ~3.8 GiB/s on 100 MiB
  loopback uploads — 2.7× httpx and 4.4× aiohttp on the same host.
- H1 transport now honours explicit `Content-Length` (or a `content_length`
  attribute) on async-iterable bodies, skipping chunked framing whenever the
  size is known up front.
- `py.typed` marker so downstream type checkers pick up the public type hints.
- `SECURITY.md` with a disclosure policy.

### Changed
- Minimum supported Python is now **3.9**. Classifiers and tool configs
  (`black`, `ruff`, `mypy`) updated accordingly.

## [2.0.0] - 2026-04-19

### Added
- Custom zero-copy `asyncio.Protocol` (`FastStream`) for the HTTP/1.1 hot path,
  bypassing `asyncio.StreamReader` and removing intermediate buffer copies.
- HTTP/2 connection probing that serializes the first TLS handshake per host so
  concurrent requests multiplex onto a single H2 connection instead of racing to
  open several.
- Optional speed extras: `orjson`, `uvloop`, `h11`, `brotli`, `zstandard`
  (installable via `pip install 'hyperhttp[speed]'`).
- Streaming response API (`aiter_bytes`, `aiter_lines`, `aread`) with strict
  `Content-Length` validation.
- DNS cache with Happy Eyeballs-style dual-stack connect.
- Integration test suite covering HTTP/1.1, HTTP/2, TLS, redirects, framing
  edge cases, retries, and circuit breaker behavior.

### Changed
- **Breaking:** full rewrite of the client, connection pool, HTTP/1 parser,
  buffer pool, and H2 multiplexer. Public API surface is smaller and
  intentionally incompatible with 1.x.
- `Response.aread` now collects raw chunks and joins once with `b"".join`,
  halving memcpy work for identity-encoded bodies with a known length.
- Socket tuning: `SO_RCVBUF=2 MiB`, `SO_SNDBUF=1 MiB`, `TCP_NODELAY`,
  `SO_KEEPALIVE` enabled by default.
- Error classifier rewritten to map hyperhttp's own exception hierarchy to
  retry / circuit-breaker categories.

### Removed
- **Breaking:** legacy connection manager, old HTTP/1 and HTTP/2 protocol
  modules, and the pre-2.0 test suite.
- `setup.py` (packaging driven entirely by `pyproject.toml`).

### Fixed
- HTTP/1.1 framing: reject responses with conflicting Content-Length and
  Transfer-Encoding, or multiple Content-Length headers.
- Body truncation now raises `RemoteProtocolError` instead of returning a
  short body silently.

## [1.1.0] - 2025-04-09

### Added
- Thread safety improvements across the codebase
- Enhanced concurrency support for HTTP/2 connections

## [1.0.0] - 2025-04-05

### Added
- Initial release of HyperHTTP
- Async-first HTTP client implementation
- HTTP/2 support with multiplexing
- Advanced connection pooling
- Memory-efficient buffer pooling
- Sophisticated retry mechanisms
- Circuit breaker implementation
- Comprehensive error handling
- Performance monitoring and metrics
- Extensive documentation
- Core HTTP client functionality
- Basic documentation
- Test suite
- CI/CD pipeline

[Unreleased]: https://github.com/lmousom/hyperhttp/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/lmousom/hyperhttp/compare/v1.1.0...v2.0.0
[1.1.0]: https://github.com/lmousom/hyperhttp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/lmousom/hyperhttp/releases/tag/v1.0.0
