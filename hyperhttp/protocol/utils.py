"""
Utility functions for HTTP protocol implementations.
"""

import json
import re
import urllib.parse
from hyperhttp.utils.buffer_pool import BufferPool
from typing import Dict, Any, Tuple, Optional, Union, List, Iterator

# Regular expression patterns for HTTP parsing
HEADER_LINE_PATTERN = re.compile(rb"([^:]+):\s*(.*?)\r\n")
STATUS_LINE_PATTERN = re.compile(rb"HTTP/(\d+\.\d+)\s+(\d+)\s+(.+?)\r\n")
CHUNK_SIZE_PATTERN = re.compile(rb"^([0-9a-fA-F]+)[^\r\n]*\r\n")


def parse_url(url: str) -> Tuple[str, str, int, str]:
    """
    Parse a URL into components.
    
    Args:
        url: URL to parse
        
    Returns:
        Tuple of (scheme, hostname, port, path)
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or "http"
    hostname = parsed.netloc.split(":")[0] if ":" in parsed.netloc else parsed.netloc
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, hostname, port, path


def build_request(
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[Union[bytes, bytearray, memoryview]] = None,
) -> bytes:
    """
    Build an HTTP/1.1 request.
    
    Args:
        method: HTTP method
        path: Request path
        headers: HTTP headers
        body: Request body
        
    Returns:
        Bytes containing the complete HTTP request
    """
    # Start with the request line
    request_parts = [f"{method} {path} HTTP/1.1\r\n"]
    
    # Add headers
    for name, value in headers.items():
        request_parts.append(f"{name}: {value}\r\n")
    
    # Add empty line to mark end of headers
    request_parts.append("\r\n")
    
    # Convert to bytes
    request_bytes = "".join(request_parts).encode("latin1")
    
    # Add body if present
    if body:
        if isinstance(body, (bytes, bytearray)):
            return request_bytes + body
        else:
            # If it's a memoryview, we need to convert to bytes
            return request_bytes + body.tobytes()
    
    return request_bytes


def prepare_body(
    data: Optional[Any] = None,
    json_data: Optional[Any] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Prepare request body and determine content type.
    
    Args:
        data: Raw data to send
        json_data: JSON data to send
        
    Returns:
        Tuple of (body_bytes, content_type)
    """
    if json_data is not None:
        # JSON data takes precedence
        body = json.dumps(json_data).encode("utf-8")
        content_type = "application/json"
        return body, content_type
    
    if data is None:
        return None, None
    
    if isinstance(data, (bytes, bytearray, memoryview)):
        # Raw bytes
        return data if isinstance(data, bytes) else bytes(data), "application/octet-stream"
    
    if isinstance(data, str):
        # String data
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    
    if isinstance(data, dict):
        # Form data
        form_data = urllib.parse.urlencode(data).encode("utf-8")
        return form_data, "application/x-www-form-urlencoded"
    
    # Default: convert to string and encode
    return str(data).encode("utf-8"), "text/plain; charset=utf-8"


def parse_headers(data: bytes) -> Tuple[Dict[str, str], int]:
    """
    Parse HTTP headers from a byte buffer.
    
    Args:
        data: Buffer containing HTTP headers
        
    Returns:
        Tuple of (headers dict, end position)
    """
    headers = {}
    
    # Find the end of the headers
    headers_end = data.find(b"\r\n\r\n")
    if headers_end == -1:
        # Headers are incomplete
        return headers, 0
    
    # Parse status line if present (for responses)
    start_pos = 0
    status_match = STATUS_LINE_PATTERN.match(data)
    if status_match:
        start_pos = status_match.end()
        headers["_status_code"] = int(status_match.group(2))
        headers["_reason"] = status_match.group(3).decode("latin1")
    
    # Parse headers
    for match in HEADER_LINE_PATTERN.finditer(data, start_pos, headers_end):
        name = match.group(1).decode("latin1").lower()
        value = match.group(2).decode("latin1")
        headers[name] = value
    
    return headers, headers_end + 4  # +4 to include the \r\n\r\n


def build_headers(headers: Dict[str, str]) -> str:
    """
    Build HTTP headers string.
    
    Args:
        headers: Dictionary of headers
        
    Returns:
        String containing formatted headers
    """
    return "".join(f"{name}: {value}\r\n" for name, value in headers.items())


class ChunkedDecoder:
    """
    Decoder for HTTP chunked transfer encoding.
    
    This implements efficient parsing of chunked-encoded HTTP responses
    with minimal memory allocations.
    """
    
    def __init__(self, buffer_pool: Optional[BufferPool] = None):
        self._buffer_pool = buffer_pool
    
    async def decode(self, reader: Any) -> bytes:
        """
        Decode a chunked HTTP body.
        
        Args:
            reader: Asyncio StreamReader or similar
            
        Returns:
            Decoded body as bytes
        """
        chunks = []
        total_size = 0
        
        while True:
            # Read chunk size line
            chunk_size_line = await reader.readuntil(b"\r\n")
            
            # Parse chunk size (ignore extensions)
            chunk_size = int(chunk_size_line.split(b";")[0].strip(), 16)
            
            if chunk_size == 0:
                # Last chunk
                # Skip the final CRLF
                await reader.readexactly(2)
                break
            
            # Read chunk data
            chunk_data = await reader.readexactly(chunk_size)
            total_size += chunk_size
            chunks.append(chunk_data)
            
            # Skip the CRLF at the end of the chunk
            await reader.readexactly(2)
        
        # Combine all chunks
        if not chunks:
            return b""
        elif len(chunks) == 1:
            return chunks[0]
        else:
            if self._buffer_pool:
                # Use buffer pool for efficient memory use
                buffer = bytearray(total_size)
                pos = 0
                for chunk in chunks:
                    buffer[pos:pos + len(chunk)] = chunk
                    pos += len(chunk)
                return bytes(buffer)
            else:
                # Fallback: concatenate chunks
                return b"".join(chunks)


def format_cookies(cookies: Dict[str, str]) -> str:
    """
    Format cookies for HTTP header.
    
    Args:
        cookies: Dictionary of cookie name/value pairs
        
    Returns:
        Formatted cookie header value
    """
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def parse_content_type(content_type: str) -> Tuple[str, Dict[str, str]]:
    """
    Parse a Content-Type header into media type and parameters.
    
    Args:
        content_type: Content-Type header value
        
    Returns:
        Tuple of (media_type, parameters)
    """
    parts = content_type.split(";")
    media_type = parts[0].strip().lower()
    
    parameters = {}
    for part in parts[1:]:
        if "=" in part:
            name, value = part.split("=", 1)
            parameters[name.strip().lower()] = value.strip().strip('"')
    
    return media_type, parameters