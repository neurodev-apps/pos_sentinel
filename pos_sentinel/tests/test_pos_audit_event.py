# -*- coding: utf-8 -*-

import json
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


class TestPosAuditEvent(TransactionCase):
    """Tests for pos.audit.event immutability, hashing, and integrity."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.AuditEvent = cls.env['pos.audit.event']

    def _create_test_event(self, event_type='void_line', **kwargs):
        """Helper to create a test audit event via the public API."""
        vals = {
            'details': {'test': True, 'reason': 'unit test'},
            'amount': 1000.0,
        }
        vals.update(kwargs)
        return self.AuditEvent.create_event(event_type, vals)

    # ── Creation & Hashing ───────────────────────────────────────

    def test_create_event_generates_hash(self):
        """Event creation must produce a non-empty SHA-256 hash."""
        event = self._create_test_event()
        self.assertTrue(event.exists())
        self.assertEqual(len(event.hash), 64, "Hash must be 64-char hex")

    def test_hash_is_deterministic(self):
        """Recomputing the hash must yield the same value."""
        event = self._create_test_event()
        recomputed = event._recompute_hash()
        self.assertEqual(event.hash, recomputed)

    def test_different_events_different_hashes(self):
        """Two different events must have different hashes."""
        event1 = self._create_test_event(event_type='void_line')
        event2 = self._create_test_event(event_type='refund')
        self.assertNotEqual(event1.hash, event2.hash)

    def test_details_stored_as_json(self):
        """Event details must be valid JSON."""
        event = self._create_test_event(
            details={'product': 'Test', 'old_price': 100, 'new_price': 50}
        )
        parsed = json.loads(event.details)
        self.assertEqual(parsed['product'], 'Test')
        self.assertEqual(parsed['old_price'], 100)

    # ── Immutability ─────────────────────────────────────────────

    def test_cannot_delete(self):
        """Deleting an audit event must raise UserError."""
        event = self._create_test_event()
        with self.assertRaises(UserError):
            event.unlink()

    def test_cannot_modify(self):
        """Modifying an audit event must raise UserError."""
        event = self._create_test_event()
        with self.assertRaises(UserError):
            event.write({'risk_level': 'critical'})

    def test_cannot_duplicate(self):
        """Duplicating an audit event must raise UserError."""
        event = self._create_test_event()
        with self.assertRaises(UserError):
            event.copy()

    def test_cannot_modify_hash(self):
        """Modifying the hash without context flag must raise UserError."""
        event = self._create_test_event()
        with self.assertRaises(UserError):
            event.write({'hash': 'tampered_hash_value'})

    def test_hash_update_via_internal_method(self):
        """Hash write via _write_hash() internal method must succeed."""
        event = self._create_test_event()
        event._write_hash('a' * 64)
        event.invalidate_recordset()
        self.assertEqual(event.hash, 'a' * 64)

    def test_tampered_flag_via_internal_method(self):
        """is_tampered write via _mark_tampered() must succeed."""
        event = self._create_test_event()
        event._mark_tampered()
        event.invalidate_recordset()
        self.assertTrue(event.is_tampered)

    def test_context_flag_without_token_rejected(self):
        """Write with old context flags (no token) must be rejected."""
        event = self._create_test_event()
        with self.assertRaises(UserError):
            event.with_context(_sentinel_hash_update=True).write({
                'hash': 'fake',
            })

    # ── Event Types ──────────────────────────────────────────────

    def test_all_event_types_creatable(self):
        """Every defined event type must be creatable."""
        from ..models.pos_audit_event import EVENT_TYPES
        for etype, _label in EVENT_TYPES:
            event = self._create_test_event(event_type=etype)
            self.assertTrue(event.exists(), "Failed to create event type: %s" % etype)
            self.assertEqual(event.event_type, etype)

    # ── Integrity Cron ───────────────────────────────────────────

    def test_integrity_cron_detects_no_tampering(self):
        """Integrity cron on clean events should find zero tampered."""
        self._create_test_event()
        self._create_test_event(event_type='refund')
        self.AuditEvent._cron_verify_integrity()

        ICP = self.env['ir.config_parameter'].sudo()
        tampered = int(ICP.get_param('pos_sentinel.last_tampered_count', '0'))
        self.assertEqual(tampered, 0)

    def test_integrity_cron_detects_tampering(self):
        """Integrity cron must detect a manually tampered hash."""
        event = self._create_test_event()
        # Bypass immutability to simulate database-level tampering
        self.env.cr.execute(
            "UPDATE pos_audit_event SET hash = 'tampered' WHERE id = %s",
            (event.id,)
        )
        self.env.invalidate_all()

        self.AuditEvent._cron_verify_integrity()

        ICP = self.env['ir.config_parameter'].sudo()
        tampered = int(ICP.get_param('pos_sentinel.last_tampered_count', '0'))
        self.assertGreaterEqual(tampered, 1)

        event.invalidate_recordset()
        self.assertTrue(event.is_tampered)

    # ── Edge Cases ───────────────────────────────────────────────

    def test_empty_details(self):
        """Event with empty details must still create and hash correctly."""
        event = self.AuditEvent.create_event('other', {
            'details': {},
        })
        self.assertTrue(event.exists())
        self.assertEqual(len(event.hash), 64)

    def test_default_values(self):
        """Event with minimal vals must use correct defaults."""
        event = self.AuditEvent.create_event('session_open', {})
        self.assertEqual(event.user_id.id, self.env.uid)
        self.assertEqual(event.risk_level, 'none')
        self.assertEqual(event.risk_score, 0.0)
        self.assertEqual(event.company_id.id, self.env.company.id)
