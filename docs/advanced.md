# Advanced Features

Start with [Basic Usage](usage.md) before diving in here.

## HTTP/2

HTTP/2 is negotiated via ALPN during the TLS handshake and is **on by default**.

```python
client = hyperhttp.Client(http2=True)   # default
client = hyperhttp.Client(http2=False)  # force HTTP/1.1 only
```

When a host speaks h2, concurrent requests to that host share a single TCP
connection and multiplex as independent streams (bounded by the server's
`MAX_CONCURRENT_STREAMS`). The client transparently falls back to HTTP/1.1
for hosts that don't advertise `h2` in ALPN.

```python
async with hyperhttp.Client() as client:
    responses = await asyncio.gather(
        client.get("https://example.com/api/1"),
        client.get("https://example.com/api/2"),
        client.get("https://example.com/api/3"),
    )
```

## Connection pooling

```python
client = hyperhttp.Client(
    max_connections=200,           # global cap across all hosts
    max_keepalive_connections=32,  # per-host keepalive cap
    keepalive_expiry=120.0,        # seconds an idle connection is kept around
)
```

The pool enforces both the global and per-host cap with FIFO waiter fairness:
the oldest request waiting for a connection is served first.

You can inspect the pool at runtime:

```python
stats = client.get_pool_stats()
# {"example.com:443": {"idle": 4, "in_use": 2, "total": 6}, ...}
```

## Proxies

`hyperhttp` tunnels requests through HTTP and HTTPS proxies. SOCKS is not
supported.

### Quick start

```python
# Single proxy for both http and https targets.
async with hyperhttp.Client(proxies="http://proxy.corp:3128") as client:
    await client.get("https://api.example.com/things")

# Per-scheme routing, with basic auth baked into the URL.
async with hyperhttp.Client(
    proxies={
        "http":  "http://user:pass@proxy.corp:3128",
        "https": "http://user:pass@proxy.corp:3128",
    },
) as client:
    ...

# Only proxy http://; send https:// direct.
async with hyperhttp.Client(
    proxies={"http": "http://proxy.corp:3128", "https": None},
) as client:
    ...
```

### How traffic is routed

| Target   | Proxy scheme | Behaviour                                            |
|----------|-------------|------------------------------------------------------|
| `http://`  | `http://`   | Single hop; requests use absolute-form URIs.         |
| `http://`  | `https://`  | TLS to the proxy, then absolute-form HTTP requests.  |
| `https://` | `http://`   | `CONNECT host:port`, then TLS to the origin (ALPN).  |
| `https://` | `https://`  | TLS to the proxy, then `CONNECT`, then TLS to origin. |

HTTP/2 is honoured end-to-end when the origin ALPN-negotiates `h2` inside the
`CONNECT` tunnel. Plain-HTTP targets over a proxy are always HTTP/1.1.

### Environment variables

When `trust_env=True` (the default), these are picked up automatically:

- `HTTP_PROXY` / `http_proxy` — for `http://` targets.
- `HTTPS_PROXY` / `https_proxy` — for `https://` targets.
- `ALL_PROXY` / `all_proxy` — fallback for either scheme.
- `NO_PROXY` / `no_proxy` — comma-separated list of hosts to bypass.

`NO_PROXY` supports:

- Exact host matches (`api.internal`).
- Suffix matches with a leading dot or `*.` (`.corp` matches `svc.corp`).
- IP literals and CIDR ranges (`10.0.0.0/8`).
- Host + port pairs (`host.local:8080`).
- `*` to disable all proxying.

Set `trust_env=False` on the client to ignore the environment entirely.
Explicit `proxies=` always overrides the environment.

### Authentication

Credentials embedded in a proxy URL (`http://user:pass@host:port`) are
stripped from the network-facing URL and added as a `Proxy-Authorization:
Basic ...` header on outgoing requests. For `CONNECT`-tunnelled HTTPS the
header is sent only on the `CONNECT` line, not on the inner request.

## Event hooks

Event hooks let you observe or mutate requests and responses without
subclassing the client. They're the extension point for:

* structured request / response logging,
* OpenTelemetry / distributed tracing propagation,
* request signing (AWS SigV4, OAuth1, HMAC, etc.),
* metrics — recording per-request latencies, response sizes, status codes,
* feature flags / shadow traffic — mutating outgoing URLs at runtime.

```python
import time
import hyperhttp

async def log_request(request):
    request._start = time.monotonic()
    print(f">> {request.method} {request.url}")

async def log_response(response):
    dt = (time.monotonic() - response.request._start) * 1000
    print(f"<< {response.status_code} ({dt:.1f} ms)")

def inject_trace(request):
    request.headers["X-Trace-Id"] = new_trace_id()

async with hyperhttp.Client(
    event_hooks={
        "request":  [log_request, inject_trace],
        "response": [log_response],
    },
) as client:
    await client.get("https://api.example.com/things")
```

### Semantics

| Event      | Fires                                                         | Mutations land |
| ---------- | ------------------------------------------------------------- | -------------- |
| `request`  | Per network attempt, just before the request goes on the wire | Yes — headers/body |
| `response` | Per network attempt, after the response head is parsed        | Yes — for inspection |

* Hooks can be **sync or async**; both are awaited correctly.
* Hooks fire **per network attempt**, which means retries and Digest-auth
  round-trips each invoke the hooks independently. That's the behaviour
  time-based signing (AWS SigV4, OAuth1 timestamps) relies on.
* Hook exceptions propagate — they're intentional, not best-effort. A
  failing hook aborts the request with the hook's exception.
* The `event_hooks` dict is writable on a live client:

  ```python
  client.event_hooks["request"].append(my_new_hook)
  ```

  Useful for scoped instrumentation — add the hook, make a few calls,
  then `pop()` it.

### Request signing example

A minimal AWS-SigV4-ish sketch:

```python
import hmac, hashlib, datetime

def sign_aws(request):
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    request.headers["x-amz-date"] = ts
    canonical = f"{request.method}\n{request.url.target}\n{ts}"
    sig = hmac.new(SECRET_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    request.headers["Authorization"] = f"AWS4-HMAC-SHA256 Signature={sig}"

async with hyperhttp.Client(event_hooks={"request": [sign_aws]}) as client:
    ...
```

Because hooks fire per attempt, a retry automatically re-signs with a
fresh timestamp — no extra plumbing required.

## Authentication

```python
import hyperhttp
from hyperhttp import BasicAuth, BearerAuth, DigestAuth
```

Three built-in schemes plus a convenient shorthand. All of them plug into
both the client default (`auth=` on the constructor) and per-request
(`auth=` on `get`/`post`/etc.). Passing `auth=None` on a single call
disables a client-level default for that call only.

### Basic (RFC 7617)

```python
async with hyperhttp.Client(auth=("alice", "s3cret")) as client:  # tuple shorthand
    r = await client.get("https://api.example.com/me")

async with hyperhttp.Client(auth=BasicAuth("alice", "s3cret")) as client:
    ...
```

### Bearer (RFC 6750)

```python
async with hyperhttp.Client(auth=BearerAuth("abc.def.ghi")) as client:
    ...
```

This is a static token scheme. There's no built-in refresh handling — wire
your own refresh loop around `Client` if you need OAuth2-style rotation.

### Digest (RFC 7616)

```python
async with hyperhttp.Client(auth=DigestAuth("Mufasa", "Circle Of Life")) as client:
    r = await client.get("https://legacy.example.com/private")
```

`DigestAuth` transparently handles the `401 → WWW-Authenticate → retry`
round-trip. Supported algorithms: `MD5`, `SHA-256`, `SHA-512-256`, and
their `-sess` variants. `qop=auth` is supported; `qop=auth-int` (body
integrity) is not — servers that require it will respond with 401 after
the retry, which surfaces to the caller as an ordinary 401 response.

The nonce-count (`nc=`) is tracked per `DigestAuth` instance and increments
automatically whenever the same nonce is reused. Reset happens when the
server issues a fresh nonce.

### Per-request control

```python
async with hyperhttp.Client(auth=BasicAuth("u", "p")) as client:
    await client.get("https://api.example.com/me")            # uses BasicAuth
    await client.get("https://api.example.com/public", auth=None)          # no auth
    await client.get("https://other.example.com/", auth=("x", "y"))        # override
```

### Custom schemes

Subclass `hyperhttp.Auth` and implement `auth_flow`, a generator that
yields `Request` objects and receives `Response` objects back via
`.send()`:

```python
from hyperhttp import Auth

class ApiKeyAuth(Auth):
    def __init__(self, key: str) -> None:
        self._key = key

    def auth_flow(self, request):
        request.headers["X-API-Key"] = self._key
        yield request
```

For challenge-response schemes (like Digest) the generator yields again
after inspecting the response:

```python
class MyChallengeAuth(Auth):
    requires_response = True
    def auth_flow(self, request):
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = compute(...)
            yield request
```

## File uploads

HyperHTTP ships a streaming `multipart/form-data` encoder that pre-computes
`Content-Length` whenever every part's size is known. That means large
uploads go out with `Content-Length` framing instead of chunked encoding
(many servers prefer or require this), and the client reads directly from
disk in 1 MiB chunks — a 10 GiB upload uses O(chunk) memory.

### Quick usage

```python
import pathlib, hyperhttp

async with hyperhttp.Client() as client:
    r = await client.post(
        "https://api.example.com/upload",
        data={"user": "alice", "note": "quarterly report"},
        files={
            "avatar": ("me.png", b"\x89PNG...", "image/png"),
            "report": pathlib.Path("./report.pdf"),
        },
    )
```

`data=` supplies the text fields; `files=` supplies the file parts. Both
are merged into a single `multipart/form-data` body. If you only pass
`data=`, it's URL-encoded as before — multipart only kicks in when `files=`
is set (or a pre-built `MultipartEncoder` is passed).

### Supported part shapes

Each value in `files=` can be:

| Shape                                        | Notes                                              |
| -------------------------------------------- | -------------------------------------------------- |
| `bytes` / `bytearray` / `memoryview`         | In-memory payload, no filename.                    |
| `pathlib.Path` or `str` path                 | Streamed from disk. Filename from the path.        |
| open binary file (`open(p, "rb")`)           | Streamed from the handle, single-use.              |
| `(filename, content)`                        | 2-tuple. `content_type` inferred from filename.    |
| `(filename, content, content_type)`          | 3-tuple with explicit `Content-Type`.              |
| `hyperhttp.MultipartFile(...)`               | Full control: path / file / content, size, ctype.  |

`content` inside a tuple or `MultipartFile` can itself be any of the basic
types above, plus an async iterable of bytes (streamed as-is).

### Pre-building the encoder

For advanced cases you can build the encoder yourself and pass it as
`content=`:

```python
from hyperhttp import Client, MultipartEncoder, MultipartFile

encoder = MultipartEncoder([
    ("user", "alice"),
    ("file", MultipartFile(path="./big.bin", content_type="application/octet-stream")),
], chunk_size=2 * 1024 * 1024)

async with Client() as client:
    await client.post("https://example.com/upload", content=encoder)
```

The `Content-Type: multipart/form-data; boundary=...` and `Content-Length`
headers are added automatically. `encoder.content_length` is `None` for
bodies that include an async iterable of unknown size; in that case the
request falls back to `Transfer-Encoding: chunked`.

### Performance notes

* Each part's header block is rendered exactly once at construction, so
  iteration is a pure bytes-fanout with no string formatting on the hot
  path.
* Disk reads use `asyncio.to_thread(fh.read, chunk)` so the event loop is
  never blocked; on Linux we hint the kernel with
  `posix_fadvise(SEQUENTIAL)` for aggressive read-ahead.
* `chunk_size` defaults to **1 MiB** — big enough that the per-chunk
  thread-hop and drain cost are negligible vs the cost of moving the
  bytes, small enough to stay well below the socket send buffer so
  backpressure still works for slow consumers.
* If any part has unknown size, the whole body falls back to chunked
  encoding automatically.

## Retry policy

Retries are opt-in. Pass a `RetryPolicy` to `Client(retry=...)`:

```python
from hyperhttp import Client
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import ExponentialBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"],
    status_force_list=[429, 500, 502, 503, 504],
    backoff_strategy=ExponentialBackoff(
        base=0.1,         # initial delay (seconds)
        factor=2.0,       # multiplier per attempt
        max_backoff=30.0, # cap on any single wait
        jitter=True,
    ),
    respect_retry_after=True,
)

async with Client(retry=retry_policy) as client:
    response = await client.get("https://api.example.com/things")
```

Decorrelated jitter usually behaves better than classic exponential backoff
under load:

```python
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    backoff_strategy=DecorrelatedJitterBackoff(base=0.1, max_backoff=10.0),
)
```

The **first** failure always surfaces the original typed exception — e.g.
`ReadTimeout`, `ConnectError`. `RetryError` is only raised once at least one
retry has actually been attempted, with the underlying exception available on
`.original_exception`.

Retries can also be disabled per request:

```python
await client.post(url, json=payload, retry=False)
```

## Circuit breaker

When a host keeps failing, the circuit breaker opens and fails fast instead of
piling more timeouts on a sick server:

```python
from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager

cb = DomainCircuitBreakerManager(
    failure_threshold=5,     # consecutive failures before opening
    recovery_timeout=30.0,   # seconds before allowing a probe request
    success_threshold=2,     # consecutive successes to fully close again
)

async with Client(circuit_breaker_manager=cb) as client:
    try:
        response = await client.get("https://api.example.com/things")
    except hyperhttp.CircuitBreakerOpen as e:
        print(f"{e.host} unhealthy for another {e.remaining:.1f}s")
```

Circuit breakers are tracked per host-port. Only the error categories listed in
`DomainCircuitBreakerManager` (by default `CONNECTION`, `TIMEOUT`, `SERVER`,
and `TRANSIENT`) count toward the failure threshold.

## Telemetry

Hook into every retry attempt for metrics/logging:

```python
from hyperhttp.errors.telemetry import ErrorTelemetry

class MyTelemetry(ErrorTelemetry):
    def record_attempt(self, retry_state, outcome):
        # outcome is "success" | "retry" | "fail"
        ...

async with Client(telemetry=MyTelemetry()) as client:
    ...
```

## TLS

```python
import ssl

ctx = ssl.create_default_context()
ctx.load_cert_chain("client-cert.pem", "client-key.pem")

client = hyperhttp.Client(ssl_context=ctx)
```

Quick toggles:

```python
hyperhttp.Client(verify=False)                       # skip verification (dev only)
hyperhttp.Client(verify="/path/to/ca-bundle.pem")    # custom CA bundle
hyperhttp.Client(cert=("client.pem", "client.key"))  # mutual TLS
```

## DNS and Happy Eyeballs

DNS results are cached with bounded TTL, and dual-stack hosts race IPv6 vs.
IPv4 with a configurable stagger:

```python
client = hyperhttp.Client(
    happy_eyeballs_delay=0.25,  # seconds to wait before racing the other family
    connect_timeout=10.0,
)
```

Set `happy_eyeballs_delay` to `0` to race both families immediately; set it
high to effectively prefer IPv6.

## Custom user agent

```python
client = hyperhttp.Client(user_agent="my-service/1.2.3")
```

Defaults to `hyperhttp/<version>`.

## Next

- [Performance Tips](performance.md)
- [API Reference](api/client.md)
- [Retry Policy](api/retry.md)
- [Errors](api/errors.md)
