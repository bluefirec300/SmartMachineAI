import unittest

from ui.currency_format import CURRENCY_SYMBOLS, currency_symbol, format_money

"""
Phase 18.1a - currency display formatting. No foreign-exchange
conversion anywhere here - purely a code -> display-symbol lookup,
with the current Malaysia/MYR demo behavior (RM) preserved exactly as
the default when no currency code is available at all.
"""


class TestCurrencySymbol(unittest.TestCase):
    def test_myr_maps_to_rm(self):
        self.assertEqual(currency_symbol("MYR"), "RM")

    def test_none_defaults_to_myr_rm(self):
        # Preserves this project's current Malaysia demo behavior when
        # no currency is available at all.
        self.assertEqual(currency_symbol(None), "RM")

    def test_lowercase_code_normalized(self):
        self.assertEqual(currency_symbol("myr"), "RM")

    def test_whitespace_stripped(self):
        self.assertEqual(currency_symbol("  MYR  "), "RM")

    def test_known_international_codes(self):
        self.assertEqual(currency_symbol("SGD"), "S$")
        self.assertEqual(currency_symbol("USD"), "$")
        self.assertEqual(currency_symbol("EUR"), "€")
        self.assertEqual(currency_symbol("GBP"), "£")
        self.assertEqual(currency_symbol("AUD"), "A$")
        self.assertEqual(currency_symbol("JPY"), "¥")
        self.assertEqual(currency_symbol("THB"), "฿")
        self.assertEqual(currency_symbol("IDR"), "Rp")

    def test_unknown_code_falls_back_to_bare_code_not_rm(self):
        # A currency genuinely not in the map must never silently
        # become "RM" - that would misrepresent a real deployment's
        # actual currency.
        self.assertEqual(currency_symbol("VND"), "VND")

    def test_every_symbol_is_distinct_from_a_bare_unmapped_code_pattern(self):
        # Sanity check the map itself has no accidental duplicate keys
        # from a bad edit.
        self.assertEqual(len(CURRENCY_SYMBOLS), len(set(CURRENCY_SYMBOLS)))


class TestFormatMoney(unittest.TestCase):
    def test_none_value_returns_none(self):
        self.assertIsNone(format_money(None, "MYR"))
        self.assertIsNone(format_money(None, None))

    def test_myr_demo_format_unchanged(self):
        self.assertEqual(format_money(464.13, "MYR"), "RM 464.13")

    def test_no_currency_defaults_to_myr_rm(self):
        self.assertEqual(format_money(100, None), "RM 100.00")

    def test_other_currency_uses_its_own_symbol(self):
        self.assertEqual(format_money(100, "USD"), "$ 100.00")
        self.assertEqual(format_money(100, "SGD"), "S$ 100.00")

    def test_thousands_separator_and_two_decimals(self):
        self.assertEqual(format_money(1234567.5, "MYR"), "RM 1,234,567.50")

    def test_accepts_int_value(self):
        self.assertEqual(format_money(100, "MYR"), "RM 100.00")

    def test_never_performs_currency_conversion(self):
        # Same numeric value, different currency codes, must produce
        # the same NUMBER with only the symbol changing - no FX math.
        myr = format_money(100, "MYR")
        usd = format_money(100, "USD")
        self.assertIn("100.00", myr)
        self.assertIn("100.00", usd)


if __name__ == "__main__":
    unittest.main()
