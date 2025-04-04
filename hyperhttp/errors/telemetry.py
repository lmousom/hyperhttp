"""
Error telemetry for tracking and analyzing HTTP errors.
"""

import asyncio
import collections
import logging
import time
from typing import Dict, Any, Optional, List, Set, Deque, DefaultDict, Counter

# Logger
logger = logging.getLogger("hyperhttp.errors.telemetry")


class ErrorTelemetry:
    """
    Error telemetry collector and analyzer.
    
    This collects and analyzes error data to provide insights into
    error patterns and help optimize retry policies.
    """
    
    def __init__(self):
        # Statistics per domain
        self._domain_stats: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._expiry_task: Optional[asyncio.Task] = None
        
    async def record_error(
        self,
        domain: str,
        error_category: str,
        response: Optional[Any] = None,
    ) -> None:
        """
        Record an error occurrence for telemetry.
        
        Args:
            domain: Domain where the error occurred
            error_category: Category of the error
            response: Optional response object
        """
        async with self._lock:
            # Initialize domain stats if needed
            if domain not in self._domain_stats:
                self._domain_stats[domain] = {
                    'total_errors': 0,
                    'categories': collections.Counter(),
                    'status_codes': collections.Counter(),
                    'error_timestamps': collections.deque(maxlen=100),
                    'recent_errors': collections.deque(maxlen=10),
                }
                
            stats = self._domain_stats[domain]
            stats['total_errors'] += 1
            stats['categories'][error_category] += 1
            stats['error_timestamps'].append(time.time())
            
            # Record status code if available
            if response and hasattr(response, 'status_code'):
                status_code = response.status_code
                stats['status_codes'][status_code] += 1
                
            # Store recent error details for diagnostics
            error_detail = {
                'timestamp': time.time(),
                'category': error_category,
                'status_code': getattr(response, 'status_code', None) if response else None,
            }
            stats['recent_errors'].append(error_detail)
            
        # Ensure expiry task is running
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._expire_old_data())
            
    async def get_error_rate(
        self,
        domain: str,
        window_seconds: float = 60.0,
    ) -> float:
        """
        Calculate error rate for a domain over the specified window.
        
        Args:
            domain: Domain to calculate rate for
            window_seconds: Time window in seconds
            
        Returns:
            Error rate (errors per second)
        """
        async with self._lock:
            if domain not in self._domain_stats:
                return 0.0
                
            stats = self._domain_stats[domain]
            if not stats['error_timestamps']:
                return 0.0
                
            # Count errors in window
            now = time.time()
            window_start = now - window_seconds
            recent_errors = sum(1 for ts in stats['error_timestamps'] 
                              if ts >= window_start)
                              
            # Calculate rate (errors per second)
            return recent_errors / window_seconds
            
    async def get_domain_report(self, domain: str) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive error report for a domain.
        
        Args:
            domain: Domain to report on
            
        Returns:
            Error report dictionary, or None if no data
        """
        async with self._lock:
            if domain not in self._domain_stats:
                return None
                
            stats = self._domain_stats[domain]
            
            # Calculate recent error rates
            now = time.time()
            window_errors = {
                '1min': sum(1 for ts in stats['error_timestamps'] 
                           if ts >= now - 60),
                '5min': sum(1 for ts in stats['error_timestamps'] 
                           if ts >= now - 300),
                '15min': sum(1 for ts in stats['error_timestamps'] 
                            if ts >= now - 900),
            }
            
            # Generate report
            return {
                'total_errors': stats['total_errors'],
                'error_categories': dict(stats['categories']),
                'status_codes': dict(stats['status_codes']),
                'error_rates': {
                    '1min': window_errors['1min'] / 60 if window_errors['1min'] else 0,
                    '5min': window_errors['5min'] / 300 if window_errors['5min'] else 0,
                    '15min': window_errors['15min'] / 900 if window_errors['15min'] else 0,
                },
                'recent_errors': list(stats['recent_errors']),
            }
    
    async def get_all_domains(self) -> List[str]:
        """
        Get all domains with error data.
        
        Returns:
            List of domains
        """
        async with self._lock:
            return list(self._domain_stats.keys())
    
    async def get_global_stats(self) -> Dict[str, Any]:
        """
        Get global error statistics across all domains.
        
        Returns:
            Global statistics dictionary
        """
        async with self._lock:
            # Aggregate stats across all domains
            total_errors = 0
            total_domains = len(self._domain_stats)
            categories: Counter[str] = collections.Counter()
            status_codes: Counter[int] = collections.Counter()
            domains_by_error: DefaultDict[str, List[str]] = collections.defaultdict(list)
            
            for domain, stats in self._domain_stats.items():
                total_errors += stats['total_errors']
                
                # Combine category counts
                for category, count in stats['categories'].items():
                    categories[category] += count
                    if count > 0:
                        domains_by_error[category].append(domain)
                
                # Combine status code counts
                for status, count in stats['status_codes'].items():
                    status_codes[status] += count
            
            return {
                'total_errors': total_errors,
                'total_domains': total_domains,
                'categories': dict(categories),
                'status_codes': dict(status_codes),
                'domains_by_error': {k: v for k, v in domains_by_error.items()},
            }
            
    async def _expire_old_data(self) -> None:
        """Periodically clean up old error data."""
        try:
            while True:
                await asyncio.sleep(300)  # Run every 5 minutes
                await self._perform_expiry()
        except asyncio.CancelledError:
            logger.debug("Error telemetry expiry task cancelled")
            
    async def _perform_expiry(self) -> None:
        """Remove expired error data."""
        now = time.time()
        expiry_threshold = now - 3600  # 1 hour
        
        async with self._lock:
            for domain, stats in list(self._domain_stats.items()):
                # Remove old timestamps
                timestamps = stats['error_timestamps']
                while timestamps and timestamps[0] < expiry_threshold:
                    timestamps.popleft()
                    
                # If domain has no recent errors, consider removing it
                if not timestamps:
                    # Keep domains with high lifetime error counts for longer
                    if stats['total_errors'] < 100:
                        del self._domain_stats[domain]
                        logger.debug(f"Removed error telemetry for inactive domain: {domain}")