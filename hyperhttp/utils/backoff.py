"""
Backoff strategies for retry mechanisms.
"""

import abc
import collections
import logging
import random
import time
from typing import Deque, Dict, Any, Optional

# Logger
logger = logging.getLogger("hyperhttp.utils.backoff")


class BackoffStrategy(abc.ABC):
    """
    Base class for backoff strategies.
    
    Backoff strategies determine the delay between retry attempts.
    """
    
    @abc.abstractmethod
    def calculate_backoff(self, retry_count: int) -> float:
        """
        Calculate the backoff delay for a retry attempt.
        
        Args:
            retry_count: The current retry attempt number (0-based)
            
        Returns:
            Delay in seconds
        """
        pass


class ExponentialBackoff(BackoffStrategy):
    """
    Exponential backoff strategy.
    
    This implements the classic exponential backoff algorithm with optional jitter.
    Delay = base * (factor^retry_count)
    """
    
    def __init__(
        self,
        base: float = 0.5,
        factor: float = 2.0,
        max_backoff: float = 60.0,
        jitter: bool = True,
    ):
        """
        Initialize exponential backoff.
        
        Args:
            base: Base delay in seconds
            factor: Multiplier for each retry
            max_backoff: Maximum backoff in seconds
            jitter: Whether to add randomness to prevent thundering herd
        """
        self.base = base
        self.factor = factor
        self.max_backoff = max_backoff
        self.jitter = jitter
        
    def calculate_backoff(self, retry_count: int) -> float:
        """
        Calculate exponential backoff delay.
        
        Args:
            retry_count: Current retry attempt (0-based)
            
        Returns:
            Delay in seconds
        """
        # Calculate exponential backoff: base * factor^retry_count
        backoff = self.base * (self.factor ** retry_count)
        
        # Apply maximum limit
        backoff = min(backoff, self.max_backoff)
        
        # Apply jitter (0.8-1.2x) to prevent thundering herd
        if self.jitter:
            jitter_multiplier = 0.8 + (random.random() * 0.4)  # 0.8-1.2
            backoff *= jitter_multiplier
            
        return backoff


class DecorrelatedJitterBackoff(BackoffStrategy):
    """
    Implementation of AWS-style "decorrelated jitter" backoff.
    
    This strategy performs better in high-contention scenarios by
    spreading out retries more effectively than simple exponential backoff.
    
    Formula: min(max_backoff, random(base, previous * 3))
    """
    
    def __init__(
        self,
        base: float = 0.5,
        max_backoff: float = 60.0,
        jitter_cap: Optional[float] = None,
    ):
        """
        Initialize decorrelated jitter backoff.
        
        Args:
            base: Base delay in seconds
            max_backoff: Maximum backoff in seconds
            jitter_cap: Optional cap on jitter
        """
        self.base = base
        self.max_backoff = max_backoff
        self.jitter_cap = jitter_cap or max_backoff
        self.previous_backoff = 0.0
        
    def calculate_backoff(self, retry_count: int) -> float:
        """
        Calculate decorrelated jitter backoff delay.
        
        Args:
            retry_count: Current retry attempt (0-based)
            
        Returns:
            Delay in seconds
        """
        if retry_count == 0:
            # First retry uses base delay
            backoff = random.uniform(0, self.base)
        else:
            # temp = min(max_backoff, random(previous * 3))
            temp = min(
                self.max_backoff,
                random.uniform(self.base, self.previous_backoff * 3)
            )
            # Cap the jitter if configured
            if self.jitter_cap:
                temp = min(temp, self.jitter_cap)
            backoff = temp
            
        self.previous_backoff = backoff
        return backoff


class AdaptiveBackoff(BackoffStrategy):
    """
    Adaptive backoff based on observed error frequencies.
    
    This strategy adjusts backoff times based on the frequency of errors,
    applying more aggressive backoff when errors are occurring frequently.
    """
    
    def __init__(
        self,
        base: float = 0.5,
        max_backoff: float = 60.0,
        window_size: int = 10,
    ):
        """
        Initialize adaptive backoff.
        
        Args:
            base: Base delay in seconds
            max_backoff: Maximum backoff in seconds
            window_size: Window size for error frequency calculation
        """
        self.base = base
        self.max_backoff = max_backoff
        self.error_history: Deque[float] = collections.deque(maxlen=window_size)
        
    def calculate_backoff(self, retry_count: int) -> float:
        """
        Calculate adaptive backoff delay.
        
        Args:
            retry_count: Current retry attempt (0-based)
            
        Returns:
            Delay in seconds
        """
        now = time.monotonic()
        self.error_history.append(now)
        
        # Calculate error frequency (errors per second)
        if len(self.error_history) >= 2:
            window = now - self.error_history[0]
            if window > 0:
                frequency = len(self.error_history) / window
                
                # Adjust backoff based on frequency
                # Higher frequency -> more aggressive backoff
                if frequency > 10:  # More than 10 errors per second
                    acceleration_factor = 4.0
                elif frequency > 5:
                    acceleration_factor = 2.0
                elif frequency > 1:
                    acceleration_factor = 1.5
                else:
                    acceleration_factor = 1.0
                    
                backoff = self.base * (acceleration_factor ** retry_count)
                return min(backoff, self.max_backoff)
        
        # Fall back to simple exponential if not enough history
        return min(self.base * (2 ** retry_count), self.max_backoff)