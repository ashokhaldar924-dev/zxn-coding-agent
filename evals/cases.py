"""Eight small but real repair tasks with hidden-from-prompt unit-test oracles."""

from __future__ import annotations

import textwrap


def _s(value: str) -> str:
    return textwrap.dedent(value).lstrip()


CASES = [
    {
        "name": "percentage-pricing",
        "task": "Fix the pricing calculation. Inspect the project and failing tests, do not modify tests, and verify the final implementation.",
        "files": {
            "pricing.py": _s("""
                def final_price(subtotal, discount_percent):
                    if subtotal < 0:
                        raise ValueError("subtotal must be non-negative")
                    if not 0 <= discount_percent <= 100:
                        raise ValueError("discount_percent must be between 0 and 100")
                    return round(subtotal * (1 - discount_percent), 2)
            """),
            "tests/test_pricing.py": _s("""
                import unittest
                from pricing import final_price

                class PricingTests(unittest.TestCase):
                    def test_percentage_discount(self):
                        self.assertEqual(final_price(200, 15), 170.0)
                    def test_boundaries(self):
                        self.assertEqual(final_price(50, 0), 50.0)
                        self.assertEqual(final_price(50, 100), 0.0)
            """),
        },
    },
    {
        "name": "half-open-intervals",
        "task": "Repair the interval overlap logic for half-open intervals. Keep the tests unchanged and run the required verifier.",
        "files": {
            "intervals.py": _s("""
                def overlaps(left, right):
                    a_start, a_end = left
                    b_start, b_end = right
                    if a_start > a_end or b_start > b_end:
                        raise ValueError("invalid interval")
                    return a_start <= b_end and b_start <= a_end
            """),
            "tests/test_intervals.py": _s("""
                import unittest
                from intervals import overlaps

                class IntervalTests(unittest.TestCase):
                    def test_touching_half_open_intervals_do_not_overlap(self):
                        self.assertFalse(overlaps((1, 3), (3, 5)))
                    def test_real_overlap(self):
                        self.assertTrue(overlaps((1, 4), (3, 5)))
            """),
        },
    },
    {
        "name": "inventory-boundary",
        "task": "Fix the inventory reservation boundary bug without changing tests. Preserve validation and verify all tests.",
        "files": {
            "inventory.py": _s("""
                class Inventory:
                    def __init__(self, stock):
                        self.stock = stock

                    def reserve(self, amount):
                        if amount <= 0:
                            raise ValueError("amount must be positive")
                        if amount >= self.stock:
                            raise ValueError("insufficient stock")
                        self.stock -= amount
                        return self.stock
            """),
            "tests/test_inventory.py": _s("""
                import unittest
                from inventory import Inventory

                class InventoryTests(unittest.TestCase):
                    def test_can_reserve_all_remaining_stock(self):
                        item = Inventory(3)
                        self.assertEqual(item.reserve(3), 0)
                    def test_cannot_over_reserve(self):
                        with self.assertRaises(ValueError):
                            Inventory(3).reserve(4)
            """),
        },
    },
    {
        "name": "config-none-merge",
        "task": "Correct configuration merging so absent optional values do not erase defaults. Do not edit tests; verify the result.",
        "files": {
            "config_merge.py": _s("""
                def merge_config(defaults, overrides):
                    merged = dict(defaults)
                    merged.update(overrides)
                    return merged
            """),
            "tests/test_config_merge.py": _s("""
                import unittest
                from config_merge import merge_config

                class ConfigMergeTests(unittest.TestCase):
                    def test_none_does_not_erase_default(self):
                        self.assertEqual(merge_config({"timeout": 30}, {"timeout": None}), {"timeout": 30})
                    def test_real_override_is_applied(self):
                        self.assertEqual(merge_config({"timeout": 30}, {"timeout": 5}), {"timeout": 5})
            """),
        },
    },
    {
        "name": "pagination-offset",
        "task": "Find and fix the one-based pagination offset error. Leave tests untouched and finish with the configured verifier.",
        "files": {
            "pagination.py": _s("""
                def page_slice(items, page, page_size):
                    if page < 1 or page_size < 1:
                        raise ValueError("page and page_size must be positive")
                    start = page * page_size
                    return items[start:start + page_size]
            """),
            "tests/test_pagination.py": _s("""
                import unittest
                from pagination import page_slice

                class PaginationTests(unittest.TestCase):
                    def test_first_page(self):
                        self.assertEqual(page_slice(list(range(7)), 1, 3), [0, 1, 2])
                    def test_second_page(self):
                        self.assertEqual(page_slice(list(range(7)), 2, 3), [3, 4, 5])
            """),
        },
    },
    {
        "name": "cache-expiration",
        "task": "Repair the cache expiration behavior using evidence from the tests. Do not modify tests and verify the final code.",
        "files": {
            "cache.py": _s("""
                class Cache:
                    def __init__(self, clock):
                        self.clock = clock
                        self.values = {}

                    def put(self, key, value, ttl):
                        self.values[key] = (value, self.clock() + ttl)

                    def get(self, key):
                        value, expires = self.values[key]
                        if self.clock() < expires:
                            return None
                        return value
            """),
            "tests/test_cache.py": _s("""
                import unittest
                from cache import Cache

                class CacheTests(unittest.TestCase):
                    def test_value_before_and_after_expiration(self):
                        now = [10]
                        cache = Cache(lambda: now[0])
                        cache.put("k", "v", 5)
                        self.assertEqual(cache.get("k"), "v")
                        now[0] = 15
                        self.assertIsNone(cache.get("k"))
            """),
        },
    },
    {
        "name": "slug-normalization",
        "task": "Make slug generation satisfy the project behavior for punctuation and repeated separators. Keep tests unchanged and verify.",
        "files": {
            "slug.py": _s("""
                def slugify(value):
                    return value.lower().replace(" ", "-")
            """),
            "tests/test_slug.py": _s("""
                import unittest
                from slug import slugify

                class SlugTests(unittest.TestCase):
                    def test_punctuation_and_repeated_spaces(self):
                        self.assertEqual(slugify(" Hello,   World! "), "hello-world")
                    def test_already_clean(self):
                        self.assertEqual(slugify("agent-design"), "agent-design")
            """),
        },
    },
    {
        "name": "retry-schedule",
        "task": "Fix the retry delay schedule so attempts and retries are not confused. Do not change tests; run the final verifier.",
        "files": {
            "retry.py": _s("""
                def retry_delays(attempts, base=1):
                    if attempts < 1:
                        raise ValueError("attempts must be positive")
                    return [base * (2 ** index) for index in range(attempts)]
            """),
            "tests/test_retry.py": _s("""
                import unittest
                from retry import retry_delays

                class RetryTests(unittest.TestCase):
                    def test_delays_exist_between_attempts_only(self):
                        self.assertEqual(retry_delays(4, base=2), [2, 4, 8])
                    def test_one_attempt_has_no_retry_delay(self):
                        self.assertEqual(retry_delays(1), [])
            """),
        },
    },
]
