"""Real local store and claims in temporary directories, synthetic tokens only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram_setup.newbot_store import NewBotClaims, OperatorApprovals, public_receipt
from telegram_setup.newbot_token_files import FileBotTokenStore, TokenStoreError
from test_sensitive_peer import TOKEN

AGENT, NAME, USERNAME, REQUEST = 'a', 'Synthetic', 'SyntheticLevBot', 'newbot-001'


class NewBotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.vault = FileBotTokenStore(self.root / 'vault')
        self.approvals = OperatorApprovals(self.root / 'approvals')
        self.claims = NewBotClaims(self.root / 'claims')

    def tearDown(self): self.tmp.cleanup()

    def test_approval_is_exact_bounded_and_single_use(self):
        self.approvals.issue(AGENT, NAME, USERNAME, REQUEST)
        self.assertEqual(self.approvals.check(AGENT, NAME, USERNAME, REQUEST)['request_id'], REQUEST)
        with self.assertRaises(PermissionError): self.approvals.consume(AGENT, NAME, 'OtherFixtureBot', REQUEST)
        self.approvals.consume(AGENT, NAME, USERNAME, REQUEST)
        with self.assertRaises(PermissionError): self.approvals.consume(AGENT, NAME, USERNAME, REQUEST)
        with self.assertRaises(ValueError): self.approvals.issue(AGENT, NAME, USERNAME, REQUEST)

    def test_expiry_and_changed_link_fail_closed(self):
        with patch('telegram_setup.newbot_store.time.time', return_value=1000):
            self.approvals.issue(AGENT, NAME, USERNAME, REQUEST, ttl=10)
        with patch('telegram_setup.newbot_store.time.time', return_value=1010):
            with self.assertRaises(PermissionError): self.approvals.check(AGENT, NAME, USERNAME, REQUEST)
        other = OperatorApprovals(self.root / 'other-approvals')
        other.issue(AGENT, NAME, USERNAME, REQUEST)
        link = os.link
        def swap(source, destination, **kwargs):
            link(source, destination, **kwargs)
            Path(destination).write_text(json.dumps({'binding': 'changed', 'expires_at': 9999999999}))
        with patch('telegram_setup.newbot_store.os.link', side_effect=swap):
            with self.assertRaises(PermissionError): other.consume(AGENT, NAME, USERNAME, REQUEST)
        with self.assertRaises(PermissionError): other.check(AGENT, NAME, USERNAME, REQUEST)

    def test_claim_survives_restart_and_prevents_any_blind_repeat(self):
        self.approvals.issue(AGENT, NAME, USERNAME, REQUEST)
        with self.claims as claims:
            first, created = claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            self.assertTrue(created); self.assertNotIn(TOKEN, json.dumps(first))
        with NewBotClaims(self.root / 'claims') as claims:
            replay, created = claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            self.assertFalse(created); self.assertEqual(replay['status'], 'uncertain_do_not_retry')
            self.approvals.issue(AGENT, NAME, 'AnotherFixtureBot', 'newbot-002')
            with self.assertRaises(PermissionError):
                claims.begin(AGENT, NAME, 'AnotherFixtureBot', 'newbot-002', self.approvals)
        self.assertNotIn('reserved_ref', public_receipt(first))

    def test_success_receipt_exposes_ref_only_and_username_cannot_be_reused(self):
        self.approvals.issue(AGENT, NAME, USERNAME, REQUEST)
        with self.claims as claims:
            claim, _ = claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            ref = self.vault.for_intent(claim['token_intent']).store(TOKEN)
            self.assertEqual(ref, claim['reserved_ref'])
            result = claims.finish_verified(claim, 123456789, USERNAME)
            public = public_receipt(result)
            self.assertEqual(public['status'], 'created_verified')
            self.assertEqual(public['token_ref'], ref)
            self.assertNotIn(TOKEN, json.dumps(public))
            replay, created = claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            self.assertFalse(created); self.assertEqual(public_receipt(replay), public)
            self.approvals.issue(AGENT, NAME, USERNAME.lower(), 'newbot-002')
            with self.assertRaises(PermissionError):
                claims.begin(AGENT, NAME, USERNAME.lower(), 'newbot-002', self.approvals)

    def test_different_plan_cannot_reuse_request_id(self):
        self.approvals.issue(AGENT, NAME, USERNAME, REQUEST)
        with self.claims as claims:
            claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            with self.assertRaises(ValueError): claims.begin(AGENT, 'Different', USERNAME, REQUEST, self.approvals)

    def test_failed_or_unverified_vault_write_cannot_produce_success(self):
        self.approvals.issue(AGENT, NAME, USERNAME, REQUEST)
        with self.claims as claims:
            claim, _ = claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            with self.assertRaises(TokenStoreError): self.vault.for_intent(claim['token_intent']).store('not-a-token')
        with NewBotClaims(self.root / 'claims') as claims:
            replay, new = claims.begin(AGENT, NAME, USERNAME, REQUEST, self.approvals)
            self.assertFalse(new); self.assertEqual(replay['status'], 'uncertain_do_not_retry')
