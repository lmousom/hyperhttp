# Installation

## Requirements

- **Python 3.8 or later**
- No required system dependencies

HyperHTTP is pure-Python. Optional C extensions (`uvloop`, `orjson`, `brotli`,
`zstandard`) are picked up automatically at import time if they are installed.

## Install from PyPI

```bash
pip install hyperhttp
```

## Optional extras

| Extra   | Adds                                                        | When to use                                    |
|---------|-------------------------------------------------------------|------------------------------------------------|
| `speed` | `uvloop`, `orjson`, `h11`, `brotli`, `zstandard`            | Production / benchmarking — recommended.       |
| `bench` | `aiohttp`, `httpx[http2]`, `uvloop`                         | Running the built-in benchmark script.         |
| `test`  | `pytest`, `pytest-asyncio`, `pytest-cov`, `aiohttp`, `httpx[http2]`, `trustme`, `hypercorn`, `brotli` | Running the test suite. |
| `dev`   | `black`, `isort`, `mypy`, `ruff`                            | Local development.                             |
| `doc`   | `mkdocs`, `mkdocs-material`, `mkdocstrings`                 | Building these docs.                           |

```bash
# Recommended for production deployments
pip install 'hyperhttp[speed]'
```

When `uvloop` is available, call it once at program start:

```python
import hyperhttp
hyperhttp.install_uvloop()  # no-op if uvloop isn't installed
```

## Install from source

```bash
git clone https://github.com/lmousom/hyperhttp.git
cd hyperhttp
pip install -e '.[speed,test]'
```

## Verify the installation

```python
import hyperhttp
print(hyperhttp.__version__)
print("uvloop:", hyperhttp.HAS_UVLOOP)
print("orjson:", hyperhttp.HAS_ORJSON)
print("brotli:", hyperhttp.HAS_BROTLI)
print("zstd:",   hyperhttp.HAS_ZSTANDARD)
```
