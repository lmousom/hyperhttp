import asyncio
import socket
import ssl

from hyperhttp.errors.classifier import ErrorClassifier
from hyperhttp.exceptions import (
    ConnectError,
    DNSError,
    HTTPStatusError,
    ReadError,
    ReadTimeout,
    RemoteProtocolError,
    TLSError,
    WriteTimeout,
)


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_status_categories():
    assert ErrorClassifier.categorize_status(500) == "SERVER"
    assert ErrorClassifier.categorize_status(503) == "TRANSIENT"
    assert ErrorClassifier.categorize_status(504) == "TIMEOUT"
    assert ErrorClassifier.categorize_status(408) == "TIMEOUT"
    assert ErrorClassifier.categorize_status(429) == "TRANSIENT"
    assert ErrorClassifier.categorize_status(401) == "CLIENT"
    assert ErrorClassifier.categorize_status(404) == "CLIENT"
    assert ErrorClassifier.categorize_status(200) == "TRANSIENT"


def test_response_based_takes_priority():
    err = Exception("irrelevant")
    assert ErrorClassifier.categorize(err, _Resp(502)) == "SERVER"


def test_hyperhttp_typed_exceptions():
    assert ErrorClassifier.categorize(ReadTimeout("x")) == "TIMEOUT"
    assert ErrorClassifier.categorize(WriteTimeout("x")) == "TIMEOUT"
    assert ErrorClassifier.categorize(DNSError("x")) == "DNS"
    assert ErrorClassifier.categorize(TLSError("x")) == "TLS"
    assert ErrorClassifier.categorize(ConnectError("x")) == "CONNECTION"
    assert ErrorClassifier.categorize(RemoteProtocolError("x")) == "PROTOCOL"
    assert ErrorClassifier.categorize(ReadError("x")) == "TRANSIENT"


def test_stdlib_exceptions():
    assert ErrorClassifier.categorize(asyncio.TimeoutError()) == "TIMEOUT"
    assert ErrorClassifier.categorize(TimeoutError()) == "TIMEOUT"
    assert ErrorClassifier.categorize(ConnectionRefusedError()) == "CONNECTION"
    assert ErrorClassifier.categorize(ConnectionResetError()) == "TRANSIENT"
    assert ErrorClassifier.categorize(socket.gaierror("boom")) == "DNS"
    assert ErrorClassifier.categorize(ssl.SSLError("boom")) == "TLS"


def test_message_heuristics_fallback():
    # Unknown class; classification falls back to message content.
    class Weird(BaseException):
        pass

    # BaseException doesn't match Exception isinstance; falls through to
    # message-based heuristics.
    assert ErrorClassifier.categorize(Weird("read timeout while waiting")) == "TIMEOUT"


def test_is_retryable_rules():
    assert ErrorClassifier.is_retryable("TRANSIENT")
    assert ErrorClassifier.is_retryable("TIMEOUT")
    assert ErrorClassifier.is_retryable("SERVER")
    assert not ErrorClassifier.is_retryable("CLIENT")
    assert not ErrorClassifier.is_retryable("FATAL")


def test_is_connection_error_rules():
    assert ErrorClassifier.is_connection_error("CONNECTION")
    assert ErrorClassifier.is_connection_error("TLS")
    assert ErrorClassifier.is_connection_error("PROTOCOL")
    assert not ErrorClassifier.is_connection_error("TIMEOUT")
    assert not ErrorClassifier.is_connection_error("SERVER")
