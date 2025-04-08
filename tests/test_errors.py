# Tests for error handling

import asyncio
import socket
import ssl
import unittest

from hyperhttp.errors.classifier import ErrorClassifier


class MockResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class TestErrorClassifier(unittest.TestCase):
    def test_categorize_status(self):
        # Test client errors (4xx)
        self.assertEqual(ErrorClassifier.categorize_status(400), "CLIENT")
        self.assertEqual(ErrorClassifier.categorize_status(401), "CLIENT")
        self.assertEqual(ErrorClassifier.categorize_status(403), "CLIENT")
        self.assertEqual(ErrorClassifier.categorize_status(404), "CLIENT")
        
        # Test special client errors
        self.assertEqual(ErrorClassifier.categorize_status(408), "TIMEOUT")
        self.assertEqual(ErrorClassifier.categorize_status(429), "TRANSIENT")
        
        # Test server errors (5xx)
        self.assertEqual(ErrorClassifier.categorize_status(500), "SERVER")
        self.assertEqual(ErrorClassifier.categorize_status(502), "SERVER")
        
        # Test special server errors
        self.assertEqual(ErrorClassifier.categorize_status(503), "TRANSIENT")
        self.assertEqual(ErrorClassifier.categorize_status(504), "TIMEOUT")
        
        # Test other status codes
        self.assertEqual(ErrorClassifier.categorize_status(200), "TRANSIENT")
    
    def test_categorize_with_exceptions(self):
        # Test network errors
        self.assertEqual(ErrorClassifier.categorize(ConnectionResetError()), "TRANSIENT")
        self.assertEqual(ErrorClassifier.categorize(ConnectionAbortedError()), "TRANSIENT")
        self.assertEqual(ErrorClassifier.categorize(ConnectionRefusedError()), "CONNECTION")
        self.assertEqual(ErrorClassifier.categorize(ConnectionError()), "CONNECTION")
        
        # Test timeout errors
        self.assertEqual(ErrorClassifier.categorize(asyncio.TimeoutError()), "TIMEOUT")
        self.assertEqual(ErrorClassifier.categorize(TimeoutError()), "TIMEOUT")
        self.assertEqual(ErrorClassifier.categorize(socket.timeout()), "TIMEOUT")
        
        # Test TLS errors
        self.assertEqual(ErrorClassifier.categorize(ssl.SSLError()), "TLS")
        self.assertEqual(ErrorClassifier.categorize(ssl.CertificateError()), "TLS")
        
        # Test DNS errors
        self.assertEqual(ErrorClassifier.categorize(socket.gaierror()), "DNS")
        
        # Test protocol errors
        self.assertEqual(ErrorClassifier.categorize(UnicodeError()), "PROTOCOL")
        self.assertEqual(ErrorClassifier.categorize(ValueError()), "PROTOCOL")
        
        # Test generic errors
        self.assertEqual(ErrorClassifier.categorize(OSError()), "CONNECTION")
        self.assertEqual(ErrorClassifier.categorize(IOError()), "CONNECTION")
        self.assertEqual(ErrorClassifier.categorize(Exception()), "FATAL")
        
        # Test subclass inheritance
        class CustomError(ValueError):
            pass
        self.assertEqual(ErrorClassifier.categorize(CustomError()), "PROTOCOL")
    
    def test_categorize_with_response(self):
        # Test with response object
        error = Exception("Generic error")
        response_400 = MockResponse(400)
        response_500 = MockResponse(500)
        response_503 = MockResponse(503)
        
        self.assertEqual(ErrorClassifier.categorize(error, response_400), "CLIENT")
        self.assertEqual(ErrorClassifier.categorize(error, response_500), "SERVER")
        self.assertEqual(ErrorClassifier.categorize(error, response_503), "TRANSIENT")
    
    def test_is_retryable(self):
        # Test retryable categories
        self.assertTrue(ErrorClassifier.is_retryable("TRANSIENT"))
        self.assertTrue(ErrorClassifier.is_retryable("TIMEOUT"))
        self.assertTrue(ErrorClassifier.is_retryable("PROTOCOL"))
        self.assertTrue(ErrorClassifier.is_retryable("CONNECTION"))
        self.assertTrue(ErrorClassifier.is_retryable("TLS"))
        self.assertTrue(ErrorClassifier.is_retryable("DNS"))
        self.assertTrue(ErrorClassifier.is_retryable("SERVER"))
        
        # Test non-retryable categories
        self.assertFalse(ErrorClassifier.is_retryable("CLIENT"))
        self.assertFalse(ErrorClassifier.is_retryable("FATAL"))
    
    def test_is_connection_error(self):
        # Test connection error categories
        self.assertTrue(ErrorClassifier.is_connection_error("CONNECTION"))
        self.assertTrue(ErrorClassifier.is_connection_error("TLS"))
        self.assertTrue(ErrorClassifier.is_connection_error("PROTOCOL"))
        self.assertTrue(ErrorClassifier.is_connection_error("FATAL"))
        
        # Test non-connection error categories
        self.assertFalse(ErrorClassifier.is_connection_error("TRANSIENT"))
        self.assertFalse(ErrorClassifier.is_connection_error("TIMEOUT"))
        self.assertFalse(ErrorClassifier.is_connection_error("DNS"))
        self.assertFalse(ErrorClassifier.is_connection_error("SERVER"))
        self.assertFalse(ErrorClassifier.is_connection_error("CLIENT"))
