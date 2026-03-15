# -*- coding: utf-8 -*-

from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestScoringEngine(TransactionCase):
    """Tests for the POS Sentinel Neuro-Scoring Engine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ScoringEngine = cls.env['pos.scoring.engine']
        cls.ScoringRule = cls.env['pos.scoring.rule']
        cls.AuditEvent = cls.env['pos.audit.event']

        # Deactivate default rules to test in isolation
        cls.ScoringRule.search([]).write({'active': False})

    def _create_rule(self, event_type='void_line', base_score=20.0, **kwargs):
        vals = {
            'name': f'Test Rule - {event_type}',
            'event_type': event_type,
            'base_score': base_score,
        }
        vals.update(kwargs)
        return self.ScoringRule.create(vals)

    # ── Basic Scoring ────────────────────────────────────────────

    def test_no_rules_returns_zero(self):
        """No active rules → score 0, level none."""
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_score'], 0.0)
        self.assertEqual(result['risk_level'], 'none')

    def test_basic_rule_applies_base_score(self):
        """A matching rule applies its base score."""
        self._create_rule('void_line', 25.0)
        result = self.ScoringEngine.compute_risk('void_line', {'amount': 100})
        self.assertEqual(result['risk_score'], 25.0)

    def test_non_matching_event_type(self):
        """Rule for different event type does not apply."""
        self._create_rule('refund', 50.0)
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_score'], 0.0)

    def test_multiple_rules_takes_highest(self):
        """Multiple matching rules → highest score wins."""
        self._create_rule('void_line', 15.0)
        self._create_rule('void_line', 30.0, name='Test Rule - void_line high')
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_score'], 30.0)

    # ── Amount Multiplier ────────────────────────────────────────

    def test_amount_below_threshold_no_multiplier(self):
        """Amount below threshold → no multiplier applied."""
        self._create_rule('void_line', 20.0,
                          amount_threshold=50000, amount_multiplier=3.0)
        result = self.ScoringEngine.compute_risk('void_line', {'amount': 30000})
        self.assertEqual(result['risk_score'], 20.0)

    def test_amount_above_threshold_applies_multiplier(self):
        """Amount above threshold → multiplier applied."""
        self._create_rule('void_line', 20.0,
                          amount_threshold=50000, amount_multiplier=3.0)
        result = self.ScoringEngine.compute_risk('void_line', {'amount': 60000})
        self.assertEqual(result['risk_score'], 60.0)

    # ── Frequency Multiplier ─────────────────────────────────────

    def test_frequency_below_threshold(self):
        """Few recent events → no frequency multiplier."""
        self._create_rule('void_line', 20.0,
                          frequency_window_minutes=60,
                          frequency_threshold=5,
                          frequency_multiplier=2.0)
        # Create 2 recent events (below threshold of 5)
        for _ in range(2):
            self.AuditEvent.create_event('void_line', {'details': {'test': True}})

        result = self.ScoringEngine.compute_risk('void_line', {
            'user_id': self.env.uid,
        })
        self.assertEqual(result['risk_score'], 20.0)

    def test_frequency_above_threshold(self):
        """Many recent events → frequency multiplier applied."""
        self._create_rule('void_line', 20.0,
                          frequency_window_minutes=60,
                          frequency_threshold=3,
                          frequency_multiplier=2.0)
        # Create 4 recent events (above threshold of 3)
        for _ in range(4):
            self.AuditEvent.create_event('void_line', {'details': {'test': True}})

        result = self.ScoringEngine.compute_risk('void_line', {
            'user_id': self.env.uid,
        })
        self.assertEqual(result['risk_score'], 40.0)

    # ── Score Capping ────────────────────────────────────────────

    def test_score_capped_at_100(self):
        """Score should never exceed 100."""
        self._create_rule('void_line', 80.0,
                          amount_threshold=100, amount_multiplier=5.0)
        result = self.ScoringEngine.compute_risk('void_line', {'amount': 200})
        self.assertEqual(result['risk_score'], 100.0)

    # ── Risk Level Mapping ───────────────────────────────────────

    def test_risk_level_none(self):
        self._create_rule('void_line', 5.0)
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_level'], 'none')

    def test_risk_level_low(self):
        self._create_rule('void_line', 15.0)
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_level'], 'low')

    def test_risk_level_medium(self):
        self._create_rule('void_line', 35.0)
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_level'], 'medium')

    def test_risk_level_high(self):
        self._create_rule('void_line', 65.0)
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_level'], 'high')

    def test_risk_level_critical(self):
        self._create_rule('void_line', 90.0)
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_level'], 'critical')

    # ── Integration: create_event auto-scores ────────────────────

    def test_create_event_auto_scores(self):
        """create_event() should auto-calculate risk via scoring engine."""
        self._create_rule('refund', 40.0)
        event = self.AuditEvent.create_event('refund', {
            'amount': 5000,
            'details': {'test': True},
        })
        self.assertEqual(event.risk_score, 40.0)
        self.assertEqual(event.risk_level, 'medium')

    def test_create_event_explicit_score_overrides(self):
        """Explicit risk_score in vals should bypass auto-scoring."""
        self._create_rule('void_line', 40.0)
        event = self.AuditEvent.create_event('void_line', {
            'risk_score': 5.0,
            'risk_level': 'low',
            'details': {'test': True},
        })
        self.assertEqual(event.risk_score, 5.0)
        self.assertEqual(event.risk_level, 'low')

    # ── Validation ───────────────────────────────────────────────

    def test_base_score_validation(self):
        """Base score must be 0-100."""
        with self.assertRaises(ValidationError):
            self._create_rule('void_line', 150.0)

    def test_multiplier_validation(self):
        """Multipliers must be 0-10."""
        with self.assertRaises(ValidationError):
            self._create_rule('void_line', 20.0, amount_multiplier=15.0)

    # ── Inactive Rules ───────────────────────────────────────────

    def test_inactive_rule_ignored(self):
        """Inactive rules should not affect scoring."""
        rule = self._create_rule('void_line', 50.0)
        rule.active = False
        result = self.ScoringEngine.compute_risk('void_line', {})
        self.assertEqual(result['risk_score'], 0.0)
