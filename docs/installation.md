# Installation

## Requirements

HyperHTTP requires:

- Python 3.7 or later
- No additional system dependencies

## Installing HyperHTTP

### Using pip

The easiest way to install HyperHTTP is using pip:

```bash
pip install hyperhttp
```

### Optional Dependencies

HyperHTTP provides optional dependencies for different use cases:

```bash
# For development (black, isort, mypy, flake8)
pip install hyperhttp[dev]

# For running tests (pytest, pytest-asyncio, pytest-cov)
pip install hyperhttp[test]

# For building documentation (mkdocs, mkdocs-material)
pip install hyperhttp[doc]
```

### From Source

To install the latest development version:

```bash
git clone https://github.com/lmousom/hyperhttp.git
cd hyperhttp
pip install -e .
```

## Verifying Installation

You can verify your installation by running Python and importing HyperHTTP:

```python
import hyperhttp
print(hyperhttp.__version__)
```