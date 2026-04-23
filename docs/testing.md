# Testing with `MockTransport`

`hyperhttp.MockTransport` is a zero-network transport built for tests. You
write a handler (or hand it a list of canned responses), pass it to
`Client(transport=...)`, and every request the client would normally send
over TCP/TLS is served instead by your handler — synchronously, in-process,
with no threads and no sockets.

The entire production code path runs: retries, auth, event hooks, cookies,
redirects, compression. Only the socket layer is replaced. That means your
test doubles behave like the real thing, but your test suite runs in
milliseconds and has no flakes.

## The four handler shapes

### 1. Callable handler (most common)

```python
from hyperhttp import Client, MockResponse, MockTransport

def handler(request):
    if request.url.path == "/users/1":
        return MockResponse(200, json={"id": 1, "name": "Alice"})
    return MockResponse(404)

async with Client(transport=MockTransport(handler)) as client:
    r = await client.get("https://api.example.com/users/1")
    assert r.status_code == 200
```

The handler may be **sync or async**. It receives a full `Request` with
`method`, `url`, `headers`, and `content`.

### 2. Replay sequence

Great for testing retry / backoff logic.

```python
mock = MockTransport([
    MockResponse(500),
    MockResponse(503),
    MockResponse(200, json={"ok": True}),
])
```

Responses are popped left-to-right per call. When the queue is exhausted
the transport raises `IndexError` — loud failure is better than silent
reuse.

### 3. Single response

```python
mock = MockTransport(MockResponse(204))
```

Every request gets the same reply. Handy for smoke tests and for asserting
that *some* call was made.

### 4. Route mapping

```python
mock = MockTransport({
    "GET /users":       MockResponse(200, json=[...]),
    "POST /users":      MockResponse(201),
    "DELETE /users/1":  MockResponse(204),
    "/ping":            MockResponse(200),  # defaults to GET
})
```

Exact match on `(method, url.path)`. Anything unmatched returns `404`.
For wildcards or path-parameters, see the [`Router`](#router) helper below
or dispatch inside your own callable.

## Building responses

`MockResponse` is a tiny response spec:

```python
MockResponse(200)                                 # empty 200
MockResponse(201, json={"id": 1})                 # sets content-type & length
MockResponse(200, text="héllo")                   # UTF-8 + text/plain
MockResponse(200, content=b"\x00\x01")            # raw bytes
MockResponse(200, headers={"x-foo": "bar"})       # custom headers
MockResponse(200, stream=async_iter_of_bytes())   # streamed body
```

Exactly one of `content` / `text` / `json` / `stream` can be provided
(specifying more raises `ValueError`). Explicit `headers=` always win over
the defaults inferred by `text=` / `json=`.

## Assertions

`MockTransport` records every request it serves:

```python
assert mock.call_count == 3
assert mock.last_request.method == "POST"
assert mock.last_request.url.path == "/users"
assert mock.last_request.headers["authorization"].startswith("Bearer ")

for req in mock.calls:
    print(req)

mock.reset()   # forget recorded calls; handler is untouched
```

Handlers can also inspect headers and bodies:

```python
def handler(request):
    assert request.headers["authorization"] == "Bearer token-xyz"
    assert request.content == b'{"name":"Alice"}'
    return MockResponse(201)
```

## Simulating failures

Handlers can raise any `hyperhttp` exception (or any `OSError`) to simulate
transport-level failures. These flow through the client exactly as real
failures would — including through your retry and circuit-breaker
policies.

```python
from hyperhttp import ConnectError
from hyperhttp.errors.retry import RetryPolicy

attempts = {"n": 0}

def handler(_req):
    attempts["n"] += 1
    if attempts["n"] < 3:
        raise ConnectError(f"simulated outage {attempts['n']}")
    return MockResponse(200)

retry = RetryPolicy(max_retries=3, retry_categories=["CONNECTION"])

async with Client(transport=MockTransport(handler), retry=retry) as client:
    r = await client.get("https://api.example.com/things")

assert r.status_code == 200
assert attempts["n"] == 3          # two failures, one success
```

## Router

For readable tests with multiple endpoints, use the tiny `Router`
helper:

```python
from hyperhttp import Client, MockResponse, MockTransport, Router

router = Router()
router.get("/health",   lambda _: MockResponse(200, text="ok"))
router.get("/users",    lambda _: MockResponse(200, json=[{"id": 1}]))
router.post("/users",   lambda _: MockResponse(201, json={"id": 2}))
router.delete("/users/1", lambda _: MockResponse(204))

# Chaining also works:
router = (
    Router(default=MockResponse(418))
    .get("/a", lambda _: MockResponse(200))
    .post("/a", lambda _: MockResponse(201))
)

async with Client(transport=MockTransport(router)) as client:
    ...
```

Routing is **exact match** on `(method, url.path)`. For anything fancier
(wildcards, regex, path-parameters), dispatch inside your own handler —
the whole point is that you have Python in your hands, not a DSL.

## Integration with retry / auth / hooks / cookies

Every piece of production client machinery continues to work with
`MockTransport`:

* **Retries / circuit breakers** — raise a `ConnectError`/`ReadTimeout`
  from your handler to exercise backoff.
* **`auth=`** — handlers see the final `Authorization` header (BasicAuth,
  BearerAuth). For `DigestAuth` the handler is invoked twice per logical
  request: once for the 401 challenge and once for the authed retry.
* **Event hooks** — `request` hooks fire before the handler is invoked,
  so header mutations are visible to the handler. `response` hooks fire
  after.
* **Cookies** — a `Set-Cookie` header in a `MockResponse` is extracted
  into the client's cookie jar, exactly as with a real server.
* **Redirects** — return `MockResponse(302, headers={"location": "..."})`
  to exercise the redirect handler.

This makes integration-flavoured tests (auth flow + retry + tracing hook,
say) trivially writable without standing up a server.

## A complete example: per-request Bearer refresh

```python
import time
from hyperhttp import Auth, Client, MockResponse, MockTransport, Router


class RefreshingBearer(Auth):
    def __init__(self) -> None:
        self._token: str | None = None
        self._exp: float = 0.0

    def auth_flow(self, request):
        if self._token is None or time.monotonic() > self._exp:
            token_req = request.copy(
                method="POST", url=request.url.join("/token"), content=b"refresh"
            )
            token_resp = yield token_req
            self._token = token_resp.json()["access_token"]
            self._exp = time.monotonic() + 3600
        request.headers["authorization"] = f"Bearer {self._token}"
        yield request


router = Router()
router.post("/token", lambda _: MockResponse(200, json={"access_token": "abc"}))
router.get("/me",     lambda req: MockResponse(
    200 if req.headers.get("authorization") == "Bearer abc" else 401
))

async with Client(transport=MockTransport(router), auth=RefreshingBearer()) as c:
    r = await c.get("https://api.example.com/me")
    assert r.status_code == 200
```

Zero threads, zero sockets, zero milliseconds of real latency — but every
line of production code (auth flow, header plumbing, connection lifecycle)
ran.
