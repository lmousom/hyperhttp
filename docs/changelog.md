# Changelog

All notable changes to HyperHTTP will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- N/A

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- N/A

### Security
- N/A

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
