import time
import unittest
from unittest.mock import Mock, patch, AsyncMock

import pytest

from hyperhttp.errors.retry import RetryPolicy, RetryState, RetryError
from hyperhttp.utils.backoff import BackoffStrategy, ExponentialBackoff
from hyperhttp.errors.classifier import ErrorClassifier


class TestRetryPolicy(unittest.TestCase):
    def test_retry_policy_defaults(self):
        policy = RetryPolicy()
        self.assertEqual(policy.max_retries, 3)
        self.assertEqual(policy.retry_categories, ['TRANSIENT', 'TIMEOUT', 'SERVER'])
        self.assertEqual(policy.status_force_list, [429, 500, 502, 503, 504])
        self.assertTrue(isinstance(policy.backoff_strategy, ExponentialBackoff))
        self.assertTrue(policy.respect_retry_after)
        
    def test_retry_policy_custom(self):
        backoff = ExponentialBackoff(base=0.5, max_backoff=10.0, jitter=True)
        policy = RetryPolicy(
            max_retries=5,
            retry_categories=['TIMEOUT', 'SERVER'],
            status_force_list=[500, 502],
            backoff_strategy=backoff,
            respect_retry_after=False,
            retry_interval_factor=1.5
        )
        
        self.assertEqual(policy.max_retries, 5)
        self.assertEqual(policy.retry_categories, ['TIMEOUT', 'SERVER'])
        self.assertEqual(policy.status_force_list, [500, 502])
        self.assertEqual(policy.backoff_strategy, backoff)
        self.assertFalse(policy.respect_retry_after)
        self.assertEqual(policy.retry_interval_factor, 1.5)
        
    def test_should_retry_exceed_max_retries(self):
        policy = RetryPolicy(max_retries=3)
        error = Exception("Test error")
        
        # Within max retries
        with patch.object(ErrorClassifier, 'categorize', return_value='TIMEOUT'):
            should_retry, _ = policy.should_retry(error, retry_count=2)
            self.assertTrue(should_retry)
            
        # Exceeds max retries
        with patch.object(ErrorClassifier, 'categorize', return_value='TIMEOUT'):
            should_retry, _ = policy.should_retry(error, retry_count=3)
            self.assertFalse(should_retry)
    
    def test_should_retry_non_retryable_category(self):
        policy = RetryPolicy()
        error = Exception("Test error")
        
        # Retryable category
        with patch.object(ErrorClassifier, 'categorize', return_value='TIMEOUT'):
            should_retry, _ = policy.should_retry(error, retry_count=0)
            self.assertTrue(should_retry)
            
        # Non-retryable category
        with patch.object(ErrorClassifier, 'categorize', return_value='CLIENT'):
            should_retry, _ = policy.should_retry(error, retry_count=0)
            self.assertFalse(should_retry)
    
    def test_should_retry_status_code(self):
        policy = RetryPolicy()
        error = Exception("Test error")
        
        # Status code in force list
        response = Mock(status_code=503)
        response.headers = {}
        
        with patch.object(ErrorClassifier, 'categorize', return_value='SERVER'):
            should_retry, _ = policy.should_retry(error, response, retry_count=0)
            self.assertTrue(should_retry)
            
        # Status code not in force list, but category is retryable
        response = Mock(status_code=501)
        response.headers = {}
        
        with patch.object(ErrorClassifier, 'categorize', return_value='SERVER'):
            should_retry, _ = policy.should_retry(error, response, retry_count=0)
            self.assertTrue(should_retry)
            
        # Status code not in force list, category not retryable
        response = Mock(status_code=400)
        response.headers = {}
        
        with patch.object(ErrorClassifier, 'categorize', return_value='CLIENT'):
            with patch.object(ErrorClassifier, 'is_retryable', return_value=False):
                should_retry, _ = policy.should_retry(error, response, retry_count=0)
                self.assertFalse(should_retry)
    
    def test_retry_with_retry_after_header(self):
        policy = RetryPolicy()
        error = Exception("Test error")
        
        # Retry-After as seconds
        response = Mock(status_code=503)
        response.headers = {"retry-after": "2"}
        
        with patch.object(ErrorClassifier, 'categorize', return_value='SERVER'):
            should_retry, delay = policy.should_retry(error, response, retry_count=0)
            self.assertTrue(should_retry)
            self.assertEqual(delay, 2.0)
            
        # Retry-After as HTTP date
        response = Mock(status_code=503)
        future_date = time.strftime('%a, %d %b %Y %H:%M:%S GMT', 
                                   time.gmtime(time.time() + 3))
        response.headers = {"retry-after": future_date}
        
        with patch.object(ErrorClassifier, 'categorize', return_value='SERVER'):
            should_retry, delay = policy.should_retry(error, response, retry_count=0)
            self.assertTrue(should_retry)
            # Should be approximately 3 seconds
            self.assertGreater(delay, 2.0)
            self.assertLess(delay, 4.0)
    
    def test_backoff_calculation(self):
        backoff_strategy = Mock(spec=BackoffStrategy)
        backoff_strategy.calculate_backoff.return_value = 2.0
        
        policy = RetryPolicy(
            backoff_strategy=backoff_strategy,
            retry_interval_factor=1.5
        )
        error = Exception("Test error")
        
        with patch.object(ErrorClassifier, 'categorize', return_value='TIMEOUT'):
            should_retry, delay = policy.should_retry(error, retry_count=1)
            
        self.assertTrue(should_retry)
        # Base delay from strategy (2.0) * factor (1.5)
        self.assertEqual(delay, 3.0)
        backoff_strategy.calculate_backoff.assert_called_once_with(1)


class TestRetryState(unittest.TestCase):
    def test_init(self):
        state = RetryState("GET", "https://example.com", {"timeout": 10})
        
        self.assertEqual(state.method, "GET")
        self.assertEqual(state.url, "https://example.com")
        self.assertEqual(state.original_kwargs, {"timeout": 10})
        self.assertEqual(state.attempts, [])
        self.assertIsNotNone(state.start_time)
        self.assertIsNotNone(state.request_id)
        self.assertEqual(state.modified_kwargs, {})
        
    def test_properties(self):
        state = RetryState("GET", "https://example.com", {})
        
        # Initial state
        self.assertEqual(state.attempt_count, 0)
        self.assertIsNone(state.last_error_category)
        self.assertEqual(state.total_delay, 0)
        
        # Add an attempt
        state.attempts.append({
            "category": "TIMEOUT",
            "backoff": 1.5
        })
        
        self.assertEqual(state.attempt_count, 1)
        self.assertEqual(state.last_error_category, "TIMEOUT")
        self.assertEqual(state.total_delay, 1.5)
        
        # Add another attempt
        state.attempts.append({
            "category": "SERVER",
            "backoff": 2.5
        })
        
        self.assertEqual(state.attempt_count, 2)
        self.assertEqual(state.last_error_category, "SERVER")
        self.assertEqual(state.total_delay, 4.0)
        
        # Test elapsed time (approximate)
        time_before = state.elapsed
        time.sleep(0.01)
        time_after = state.elapsed
        self.assertGreater(time_after, time_before)


class TestRetryError(unittest.TestCase):
    def test_retry_error(self):
        original_error = ValueError("Original error")
        state = RetryState("GET", "https://example.com", {})
        
        error = RetryError("All retries failed", original_error, state)
        
        self.assertEqual(error.message, "All retries failed")
        self.assertEqual(error.original_exception, original_error)
        self.assertEqual(error.retry_state, state)
        self.assertEqual(str(error), "All retries failed")
