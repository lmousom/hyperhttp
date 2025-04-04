"""
Error classification system for precise error handling.
"""

import asyncio
import ssl
import socket
import time
from typing import Dict, Any, Optional, Type, Set, Union


class ErrorClassifier:
    """
    Classifier for HTTP errors.
    
    This enables precise handling of different error types with specific
    retry policies and circuit breaker behavior.
    """
    
    # Error categories with increasing severity
    CATEGORIES = {
        'TRANSIENT': 10,  # Temporary network glitches, likely to resolve quickly
        'TIMEOUT': 20,    # Connection/read/write timeouts
        'PROTOCOL': 30,   # Protocol-level errors (malformed responses, etc.)
        'CONNECTION': 40, # Connection establishment failures
        'TLS': 50,        # TLS handshake failures
        'DNS': 60,        # DNS resolution failures
        'SERVER': 70,     # Server errors (5xx)
        'CLIENT': 80,     # Client errors (4xx)
        'FATAL': 90       # Unrecoverable errors
    }
    
    # Mapping of exception types to categories
    ERROR_MAPPING: Dict[Type[Exception], str] = {
        # Network errors
        ConnectionResetError: 'TRANSIENT',
        ConnectionAbortedError: 'TRANSIENT',
        ConnectionRefusedError: 'CONNECTION',
        ConnectionError: 'CONNECTION',
        
        # Timeout errors
        asyncio.TimeoutError: 'TIMEOUT',
        TimeoutError: 'TIMEOUT',
        socket.timeout: 'TIMEOUT',
        
        # TLS errors
        ssl.SSLError: 'TLS',
        ssl.CertificateError: 'TLS',
        
        # DNS errors
        socket.gaierror: 'DNS',
        
        # Protocol errors
        UnicodeError: 'PROTOCOL',
        ValueError: 'PROTOCOL',  # Often from malformed responses
        
        # Generic errors (will be refined based on content)
        OSError: 'CONNECTION',
        IOError: 'CONNECTION',
        Exception: 'FATAL',
    }
    
    @classmethod
    def categorize_status(cls, status_code: int) -> str:
        """
        Categorize an HTTP status code.
        
        Args:
            status_code: HTTP status code
            
        Returns:
            Error category
        """
        if 400 <= status_code < 500:
            # Special cases for certain 4xx status codes
            if status_code == 408:  # Request Timeout
                return 'TIMEOUT'
            if status_code == 429:  # Too Many Requests
                return 'TRANSIENT'
            if status_code in (401, 403):  # Authentication errors
                return 'CLIENT'
            return 'CLIENT'
        elif 500 <= status_code < 600:
            if status_code == 503:  # Service Unavailable
                return 'TRANSIENT'
            if status_code == 504:  # Gateway Timeout
                return 'TIMEOUT'
            return 'SERVER'
        return 'TRANSIENT'  # Default for other status codes
    
    @classmethod
    def categorize(cls, 
                   error: Exception,
                   response: Optional[Any] = None) -> str:
        """
        Categorize an error based on exception type and context.
        
        Args:
            error: Exception that occurred
            response: Optional response object for HTTP status errors
            
        Returns:
            Error category
        """
        # First, check for HTTP status-based errors if response exists
        if response and hasattr(response, 'status_code'):
            status_code = response.status_code
            if 400 <= status_code < 600:
                return cls.categorize_status(status_code)
                
        # Then categorize based on exception type
        error_type = type(error)
        if error_type in cls.ERROR_MAPPING:
            return cls.ERROR_MAPPING[error_type]
            
        # Check parent classes if exact type not found
        for base_cls, category in cls.ERROR_MAPPING.items():
            if isinstance(error, base_cls):
                return category
            
        # Use message content for more granular classification
        error_msg = str(error).lower()
        if 'timeout' in error_msg:
            return 'TIMEOUT'
        if 'reset' in error_msg or 'broken pipe' in error_msg:
            return 'TRANSIENT'
        if 'connection' in error_msg:
            return 'CONNECTION'
        if 'tls' in error_msg or 'ssl' in error_msg:
            return 'TLS'
        if 'dns' in error_msg or 'name resolution' in error_msg:
            return 'DNS'
                
        # Default to most severe category if unknown
        return 'FATAL'
        
    @classmethod
    def is_retryable(cls, category: str) -> bool:
        """
        Determine if errors in this category should be retried.
        
        Args:
            category: Error category
            
        Returns:
            True if retryable, False otherwise
        """
        # All categories less severe than CLIENT are considered retryable
        return cls.CATEGORIES[category] < cls.CATEGORIES['CLIENT']
    
    @classmethod
    def is_connection_error(cls, category: str) -> bool:
        """
        Determine if this category indicates the connection is bad.
        
        Args:
            category: Error category
            
        Returns:
            True if connection should be discarded, False otherwise
        """
        # These categories indicate the connection itself is bad
        connection_error_categories = {
            'CONNECTION', 'TLS', 'PROTOCOL', 'FATAL'
        }
        return category in connection_error_categories