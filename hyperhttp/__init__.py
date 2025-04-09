# Package exports
"""
HyperHTTP: High-performance HTTP client library for Python.

This library provides a high-performance HTTP client that outperforms
existing libraries like requests and httpx through advanced protocol
optimization, memory management, and connection handling.
"""

from typing import Dict, Any, List, Optional, Union, Tuple, Type

from hyperhttp.client import Client, Response

__version__ = "1.1.0"

__all__ = ["Client", "Response"]