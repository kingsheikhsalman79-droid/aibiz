"""Tests for the pure logic (no web server or database needed).
Run from the project folder:  python -m unittest discover -s tests -v
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ads_engine, llm, mailer, security  # noqa: E402

SECRET = "x" * 32
PRODUCTS = [
    {"id": 1, "name": "Blue Sneaker", "description": "Light running shoe", "details": "Sizes 38-45", "price": 49.9, "currency": "USD", "stock": 5, "sku": None},
    {"id": 2, "name": "Leather Wallet", "description": "Brown wallet", "details": "", "price": 20, "currency": "USD", "stock": 0, "sku": None},
]


class SecurityTests(unittest.TestCase):
    def test_password_roundtrip(self):
        h = security.hash_password("correct horse")
        self.assertTrue(security.verify_password("correct horse", h))
        self.assertFalse(security.verify_password("wrong", h))
        self.assertFalse(security.verify_password("x", "garbage"))

    def test_token_valid_expired_tampered(self):
        tok = security.make_token(7, SECRET, 60)
        self.assertEqual(security.read_token(tok, SECRET), 7)
        self.assertIsNone(security.read_token(tok, "y" * 32))
        self.assertIsNone(security.read_token(security.make_token(7, SECRET, -1), SECRET))
        body, sig = tok.split(".")
        self.assertIsNone(security.read_token(body + "." + sig[:-2] + "aa", SECRET))
        self.assertIsNone(security.read_token("nonsense", SECRET))

    def test_api_key(self):
        raw, prefix, digest = security.new_api_key()
        self.assertTrue(raw.startswith("sk_live_"))
        self.assertEqual(prefix, raw[:12])
        self.assertEqual(digest, security.hash_api_key(raw))
        self.assertNotIn(raw, digest)

    def test_encrypt_roundtrip(self):
        enc = security.encrypt("app-password", SECRET)
        self.assertNotIn("app-password", enc)
        self.assertEqual(security.decrypt(enc, SECRET), "app-password")
        self.assertIsNone(security.decrypt(enc, "z" * 32))


class AdsTests(unittest.TestCase):
    def codes(self, camp, m):
        return {p["code"]: p for p in ads_engine.analyze(camp, m)}

    def test_healthy(self):
        self.assertEqual(ads_engine.analyze({"daily_budget": 50, "target_cpa": 10}, {"spend": 20, "impressions": 4000, "clicks": 80, "conversions": 4}), [])

    def test_disapproved(self):
        c = self.codes({}, {"status": "disapproved", "disapproval_reason": "Misleading claim"})
        self.assertIn("AD_DISAPPROVED", c)
        self.assertIn("Misleading claim", c["AD_DISAPPROVED"]["diagnosis"])

    def test_budget_increase_needs_owner_cap(self):
        m = {"spend": 50, "impressions": 3000, "clicks": 100, "conversions": 5}
        no_cap = self.codes({"daily_budget": 50, "max_daily_budget": 0, "target_cpa": 20}, m)["BUDGET_CAPPED_GOOD_PERFORMANCE"]
        self.assertFalse(no_cap["auto"])
        capped = self.codes({"daily_budget": 50, "max_daily_budget": 55, "target_cpa": 20}, m)["BUDGET_CAPPED_GOOD_PERFORMANCE"]
        self.assertTrue(capped["auto"])
        self.assertEqual(capped["params"]["to"], 55)  # never above the owner's maximum

    def test_high_cpa_and_no_conversions(self):
        self.assertIn("HIGH_CPA", self.codes({"daily_budget": 50, "target_cpa": 10}, {"spend": 100, "impressions": 5000, "clicks": 100, "conversions": 4}))
        self.assertIn("NO_CONVERSIONS", self.codes({"daily_budget": 50, "target_cpa": 10}, {"spend": 40, "impressions": 5000, "clicks": 100, "conversions": 0}))

    def test_low_ctr_and_no_delivery(self):
        self.assertIn("LOW_CTR", self.codes({}, {"impressions": 5000, "clicks": 5}))
        self.assertIn("NO_DELIVERY", self.codes({}, {"impressions": 0, "days_running": 2}))

    def test_bad_numbers_do_not_crash(self):
        ads_engine.analyze({"daily_budget": "abc"}, {"spend": None, "impressions": "x"})

    def test_noop_adapter_only_recommends(self):
        problem = {"auto": True, "action": "pause_campaign", "params": {}}
        self.assertEqual(ads_engine.execute(ads_engine.NoopAdapter(), problem), "recommended")
        self.assertEqual(ads_engine.execute(ads_engine.NoopAdapter(), {**problem, "auto": False}), "needs_human")


class LLMTests(unittest.TestCase):
    def test_rank_puts_relevant_first(self):
        self.assertEqual(llm.rank_products(PRODUCTS, "do you have a wallet?")[0]["name"], "Leather Wallet")

    def test_demo_reply(self):
        self.assertIn("Blue Sneaker", llm.demo_reply(PRODUCTS, "price of the blue sneaker"))
        self.assertIn("Blue Sneaker", llm.demo_reply(PRODUCTS, "hello"))
        self.assertIn("team", llm.demo_reply([], "hello"))

    def test_system_prompt_contains_catalog_and_safety_rules(self):
        s = llm.build_system_prompt("Shoe Shop", "Free delivery over $50", PRODUCTS, "sneaker")
        self.assertIn("Blue Sneaker", s)
        self.assertIn("Free delivery", s)
        self.assertIn("untrusted", s)


class MailerTests(unittest.TestCase):
    def test_header_injection_blocked(self):
        with self.assertRaises(mailer.MailError):
            mailer._clean("hi\r\nBcc: evil@example.com", "subject")
        with self.assertRaises(mailer.MailError):
            mailer.send_email("me@gmail.com", "pw", "a@b.com\nBcc: x@y.com", "s", "b")

    def test_skip_rules_prevent_loops(self):
        base = {"from_addr": "buyer@example.com", "auto": False}
        self.assertFalse(mailer.should_skip(base, "shop@gmail.com"))
        self.assertTrue(mailer.should_skip({**base, "from_addr": "noreply@example.com"}, "shop@gmail.com"))
        self.assertTrue(mailer.should_skip({**base, "from_addr": "shop@gmail.com"}, "shop@gmail.com"))
        self.assertTrue(mailer.should_skip({**base, "auto": True}, "shop@gmail.com"))

    def test_parse_mail(self):
        raw = b"From: Ali <ali@example.com>\r\nSubject: Price?\r\nMessage-ID: <1@x>\r\n\r\nHow much is it?"
        m = mailer.parse_mail("5", raw)
        self.assertEqual((m["from_addr"], m["subject"], m["body"]), ("ali@example.com", "Price?", "How much is it?"))


if __name__ == "__main__":
    unittest.main()
