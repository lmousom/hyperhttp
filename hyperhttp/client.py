"""
Main client interface for HyperHTTP.
"""

import asyncio
import json
import typing
from typing import Dict, Any, List, Optional, Union, Tuple, Callable, TypeVar
from urllib.parse import urljoin

from hyperhttp.connection.pool import ConnectionPoolManager
from hyperhttp.errors.classifier import ErrorClassifier
from hyperhttp.errors.retry import RetryPolicy, RetryHandler
from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager
from hyperhttp.errors.telemetry import ErrorTelemetry
from hyperhttp.utils.buffer_pool import BufferPool

T = TypeVar("T")

class Response:
    """HTTP Response object."""
    
    def __init__(
        self,
        status_code: int,
        headers: Dict[str, str],
        body_source: Any,
        url: str,
        request_time: float,
        request_size: int = 0,
        response_size: int = 0,
    ):
        self.status_code = status_code
        self.headers = headers
        self._body_source = body_source
        self.url = url
        self.elapsed = request_time
        self.request_size = request_size
        self.response_size = response_size
        self._body = None
        self._text = None
        self._json = None
    
    async def body(self) -> bytes:
        """Get the response body as bytes."""
        if self._body is not None:
            return self._body
            
        if hasattr(self._body_source, "read"):
            self._body = await self._body_source.read()
        else:
            self._body = self._body_source
            
        return self._body
    
    async def text(self) -> str:
        """Get the response body as text."""
        if self._text is not None:
            return self._text
            
        body = await self.body()
        encoding = self._get_encoding()
        self._text = body.decode(encoding)
        return self._text
    
    async def json(self) -> Any:
        """Get the response body as JSON."""
        if self._json is not None:
            return self._json
            
        text = await self.text()
        self._json = json.loads(text)
        return self._json
    
    def _get_encoding(self) -> str:
        """Get the character encoding from Content-Type header."""
        content_type = self.headers.get("content-type", "")
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("charset="):
                return part[8:].strip().strip('"\'')
        return "utf-8"  # Default to UTF-8
    
    def raise_for_status(self) -> None:
        """Raise an exception if the response status indicates an error."""
        if 400 <= self.status_code < 600:
            raise HttpError(f"HTTP Error {self.status_code}", response=self)


class HttpError(Exception):
    """Exception raised for HTTP errors."""
    
    def __init__(self, message: str, response: Optional[Response] = None):
        self.message = message
        self.response = response
        super().__init__(message)


class Client:
    """
    High-performance HTTP client.
    
    This is the main interface for making HTTP requests.
    """
    
    def __init__(
        self,
        base_url: str = "",
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        max_connections: int = 100,
        retry_policy: Optional[RetryPolicy] = None,
        buffer_pool_size: int = 32,
    ):
        self.base_url = base_url
        self.default_headers = headers or {}
        self.default_timeout = timeout
        
        # Initialize shared components
        self._buffer_pool = BufferPool(initial_count=buffer_pool_size)
        self._pool_manager = ConnectionPoolManager(max_connections=max_connections)
        self._circuit_breakers = DomainCircuitBreakerManager()
        self._telemetry = ErrorTelemetry()
        
        # Create retry handler
        self._retry_policy = retry_policy or RetryPolicy()
        self._retry_handler = RetryHandler(
            client=self,
            retry_policy=self._retry_policy,
            circuit_breaker_manager=self._circuit_breakers,
        )
    
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Union[Dict[str, Any], str, bytes]] = None,
        json: Optional[Any] = None,
        timeout: Optional[float] = None,
        follow_redirects: bool = True,
        max_redirects: int = 10,
    ) -> Response:
        """
        Send an HTTP request.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: URL to request
            params: Query parameters
            headers: HTTP headers
            data: Request body as form data or raw bytes
            json: Request body as JSON
            timeout: Request timeout in seconds
            follow_redirects: Whether to follow redirects
            max_redirects: Maximum number of redirects to follow
            
        Returns:
            Response object
        """
        # Prepare complete URL
        if self.base_url and not url.startswith(("http://", "https://")):
            url = urljoin(self.base_url, url)
            
        # Merge headers
        merged_headers = {**self.default_headers}
        if headers:
            merged_headers.update(headers)
            
        # Prepare request options
        request_options = {
            "method": method,
            "url": url,
            "params": params,
            "headers": merged_headers,
            "data": data,
            "json": json,
            "timeout": timeout or self.default_timeout,
            "follow_redirects": follow_redirects,
            "max_redirects": max_redirects,
        }
        
        # Send request with retry handling
        try:
            return await self._retry_handler.execute_with_retry(**request_options)
        except Exception as e:
            # Add context to error
            if not isinstance(e, HttpError):
                raise HttpError(f"Error requesting {url}: {str(e)}") from e
            raise
    
    async def _execute_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Union[Dict[str, Any], str, bytes]] = None,
        json: Optional[Any] = None,
        timeout: float = 30.0,
        follow_redirects: bool = True,
        max_redirects: int = 10,
    ) -> Response:
        """Execute a single request (called by retry handler)."""
        # Get connection from pool
        connection = await self._pool_manager.get_connection(url)
        
        try:
            # Send request using connection
            start_time = asyncio.get_event_loop().time()
            
            response_dict = await connection.send_request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                data=data,
                json=json,
                timeout=timeout,
                buffer_pool=self._buffer_pool,
            )
            
            end_time = asyncio.get_event_loop().time()
            elapsed = end_time - start_time
            
            # Create response object from dictionary
            response = Response(
                status_code=response_dict["status_code"],
                headers=response_dict["headers"],
                body_source=response_dict["body_source"],
                url=url,
                request_time=elapsed,
                request_size=response_dict.get("request_size", 0),
                response_size=response_dict.get("response_size", 0),
            )
            
            # Handle redirects if needed
            if follow_redirects and 300 <= response.status_code < 400 and "location" in response.headers:
                if max_redirects <= 0:
                    raise HttpError("Too many redirects")
                    
                # Get redirect URL
                redirect_url = response.headers["location"]
                if not redirect_url.startswith(("http://", "https://")):
                    redirect_url = urljoin(url, redirect_url)
                    
                # Make redirect request (reusing most parameters)
                return await self._execute_request(
                    method="GET",  # Always use GET for redirects
                    url=redirect_url,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=follow_redirects,
                    max_redirects=max_redirects - 1,
                )
            
            # Update connection statistics with success
            connection.metadata.record_request_success(
                sent_bytes=response.request_size,
                received_bytes=response.response_size,
                rtt=elapsed,
            )
            
            # Return connection to pool
            self._pool_manager.release_connection(connection)
            
            return response
        except Exception as e:
            # Update connection statistics with failure
            if hasattr(connection, "metadata"):
                connection.metadata.record_request_failure(str(e))
            
            # Determine if connection should be recycled
            should_recycle = self._should_recycle_connection(e)
            
            # Return connection to pool (or close it)
            self._pool_manager.release_connection(connection, recycle=should_recycle)
            
            # Re-raise the exception
            raise
    
    def _should_recycle_connection(self, error: Exception) -> bool:
        """Determine if a connection can be reused after an error."""
        category = ErrorClassifier.categorize(error)
        
        # Categorize errors that invalidate the connection
        non_recyclable = {
            "CONNECTION", "TLS", "PROTOCOL", "FATAL"
        }
        
        # These errors indicate the connection itself is bad
        return category not in non_recyclable
    
    # Convenience methods for common HTTP methods
    
    async def get(
        self, url: str, *, params: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Response:
        """Send a GET request."""
        return await self.request("GET", url, params=params, **kwargs)
    
    async def post(
        self, url: str, *, data: Optional[Any] = None, json: Optional[Any] = None, **kwargs: Any
    ) -> Response:
        """Send a POST request."""
        return await self.request("POST", url, data=data, json=json, **kwargs)
    
    async def put(
        self, url: str, *, data: Optional[Any] = None, json: Optional[Any] = None, **kwargs: Any
    ) -> Response:
        """Send a PUT request."""
        return await self.request("PUT", url, data=data, json=json, **kwargs)
    
    async def patch(
        self, url: str, *, data: Optional[Any] = None, json: Optional[Any] = None, **kwargs: Any
    ) -> Response:
        """Send a PATCH request."""
        return await self.request("PATCH", url, data=data, json=json, **kwargs)
    
    async def delete(self, url: str, **kwargs: Any) -> Response:
        """Send a DELETE request."""
        return await self.request("DELETE", url, **kwargs)
    
    async def head(self, url: str, **kwargs: Any) -> Response:
        """Send a HEAD request."""
        return await self.request("HEAD", url, **kwargs)
    
    async def options(self, url: str, **kwargs: Any) -> Response:
        """Send an OPTIONS request."""
        return await self.request("OPTIONS", url, **kwargs)
    
    async def close(self) -> None:
        """Close all connections and free resources."""
        await self._pool_manager.close()
    
    async def __aenter__(self) -> "Client":
        return self
    
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()