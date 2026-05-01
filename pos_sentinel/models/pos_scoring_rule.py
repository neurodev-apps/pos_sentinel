# -*- coding: utf-8 -*-

import logging

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Thresholds for mapping score → risk_level
DEFAULT_THRESHOLDS = {
    'low': 10.0,
    'medium': 30.0,
    'high': 60.0,
    'critical': 85.0,
}


class PosScoringRule(models.Model):
    _name = 'pos.scoring.rule'
    _description = 'POS Scoring Rule'
    _order = 'sequence, id'
    _rec_name = 'name'

    name = fields.Char(
        string='Rule Name',
        required=True,
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # ── What triggers this rule ──────────────────────────────────
    event_type = fields.Selection(
        selection='_get_event_types',
        string='Event Type',
        required=True,
        index=True,
    )

    # ── Scoring ──────────────────────────────────────────────────
    base_score = fields.Float(
        string='Base Score',
        digits=(6, 2),
        required=True,
        default=10.0,
        help='Base risk score assigned when this rule matches.',
    )

    # ── Conditional multipliers ──────────────────────────────────
    amount_threshold = fields.Float(
        string='Amount Threshold',
        digits=(12, 2),
        default=0.0,
        help='If event amount exceeds this value, apply the amount multiplier. '
             '0 = no amount check.',
    )
    amount_multiplier = fields.Float(
        string='Amount Multiplier',
        digits=(4, 2),
        default=1.0,
        help='Multiply base score by this factor when amount exceeds threshold.',
    )
    frequency_window_minutes = fields.Integer(
        string='Frequency Window (min)',
        default=0,
        help='Check how many similar events occurred in this time window. '
             '0 = no frequency check.',
    )
    frequency_threshold = fields.Integer(
        string='Frequency Threshold',
        default=0,
        help='If event count in window exceeds this, apply frequency multiplier. '
             '0 = no frequency check.',
    )
    frequency_multiplier = fields.Float(
        string='Frequency Multiplier',
        digits=(4, 2),
        default=1.5,
        help='Multiply base score by this factor when frequency threshold is exceeded.',
    )
    after_hours_multiplier = fields.Float(
        string='After-Hours Multiplier',
        digits=(4, 2),
        default=1.0,
        help='Multiply score if event occurs outside business hours. '
             '1.0 = no effect.',
    )
    business_hours_start = fields.Float(
        string='Business Hours Start',
        default=8.0,
        help='Start of business hours (24h format, e.g. 8.0 = 08:00).',
    )
    business_hours_end = fields.Float(
        string='Business Hours End',
        default=22.0,
        help='End of business hours (24h format, e.g. 22.0 = 22:00).',
    )

    # ── Scope ────────────────────────────────────────────────────
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        help='Leave empty for global rule.',
    )

    @api.model
    def _get_event_types(self):
        from .pos_audit_event import EVENT_TYPES
        return EVENT_TYPES

    @api.constrains('base_score')
    def _check_base_score(self):
        for rule in self:
            if rule.base_score < 0 or rule.base_score > 100:
                raise ValidationError(_(
                    'Base score must be between 0 and 100.'
                ))

    @api.constrains('amount_multiplier', 'frequency_multiplier', 'after_hours_multiplier')
    def _check_multipliers(self):
        for rule in self:
            for field_name in ('amount_multiplier', 'frequency_multiplier', 'after_hours_multiplier'):
                val = getattr(rule, field_name)
                if val < 0.0 or val > 10.0:
                    raise ValidationError(_(
                        'Multipliers must be between 0.0 and 10.0.'
                    ))


class PosScoringEngine(models.AbstractModel):
    _name = 'pos.scoring.engine'
    _description = 'POS Scoring Engine'

    @api.model
    def compute_risk(self, event_type, vals):
        """Compute risk score and level for an event.

        Evaluates all active scoring rules matching the event_type.
        Applies conditional multipliers (amount, frequency, after-hours).
        Returns the highest score among matching rules.

        Args:
            event_type: str — event type key
            vals: dict with event data (amount, pos_session_id, user_id, etc.)

        Returns:
            dict: {'risk_score': float, 'risk_level': str}
        """
        rules = self._get_matching_rules(event_type)
        if not rules:
            return {'risk_score': 0.0, 'risk_level': 'none'}

        max_score = 0.0
        amount = abs(vals.get('amount', 0.0))

        for rule in rules:
            score = rule.base_score

            # Amount multiplier
            if rule.amount_threshold > 0 and amount > rule.amount_threshold:
                score *= rule.amount_multiplier

            # Frequency multiplier
            if rule.frequency_window_minutes > 0 and rule.frequency_threshold > 0:
                freq = self._count_recent_events(
                    event_type,
                    vals.get('user_id', self.env.uid),
                    vals.get('pos_session_id'),
                    rule.frequency_window_minutes,
                )
                if freq >= rule.frequency_threshold:
                    score *= rule.frequency_multiplier

            # After-hours multiplier
            if rule.after_hours_multiplier > 1.0:
                if self._is_after_hours(rule):
                    score *= rule.after_hours_multiplier

            max_score = max(max_score, score)

        # Cap at 100
        max_score = min(max_score, 100.0)
        risk_level = self._score_to_level(max_score)

        return {'risk_score': round(max_score, 2), 'risk_level': risk_level}

    @api.model
    def _get_matching_rules(self, event_type):
        """Get active scoring rules for an event type, scoped by company."""
        domain = [
            ('active', '=', True),
            ('event_type', '=', event_type),
            '|',
            ('company_id', '=', False),
            ('company_id', '=', self.env.company.id),
        ]
        return self.env['pos.scoring.rule'].sudo().search(domain, order='sequence')

    @api.model
    def _count_recent_events(self, event_type, user_id, session_id, window_minutes):
        """Count recent events of the same type by the same user (company-scoped)."""
        from datetime import timedelta
        cutoff = fields.Datetime.now() - timedelta(minutes=window_minutes)

        domain = [
            ('event_type', '=', event_type),
            ('user_id', '=', user_id),
            ('create_date', '>=', cutoff),
            ('company_id', 'in', self.env.companies.ids),
        ]
        if session_id:
            domain.append(('pos_session_id', '=', session_id))

        return self.env['pos.audit.event'].sudo().search_count(domain)

    @api.model
    def _is_after_hours(self, rule):
        """Check if current time is outside business hours (company timezone)."""
        now_utc = fields.Datetime.now()
        tz_name = self.env.company.partner_id.tz or self.env.user.tz or 'UTC'
        try:
            tz = pytz.timezone(tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            _logger.warning("POS Sentinel: unknown timezone '%s', defaulting to UTC", tz_name)
            tz = pytz.UTC
        now_local = pytz.UTC.localize(now_utc).astimezone(tz)
        current_hour = now_local.hour + now_local.minute / 60.0
        return current_hour < rule.business_hours_start or current_hour >= rule.business_hours_end

    @api.model
    def _evaluate_after_hours_global(self):
        """Evaluate the global after-hours config against current local time.

        Returns dict with:
            - is_after_hours: bool
            - boost: float (points to add to risk_score, 0 if disabled)
            - reason: str (for forensic context)
        """
        ICP = self.env['ir.config_parameter'].sudo()
        enabled = (ICP.get_param('pos_sentinel.after_hours_enabled') or 'True') == 'True'
        if not enabled:
            return {'is_after_hours': False, 'boost': 0.0, 'reason': 'disabled'}

        try:
            start = float(ICP.get_param('pos_sentinel.business_hours_start') or 8.0)
            end = float(ICP.get_param('pos_sentinel.business_hours_end') or 22.0)
            boost = float(ICP.get_param('pos_sentinel.after_hours_boost') or 25.0)
        except (TypeError, ValueError):
            start, end, boost = 8.0, 22.0, 25.0
        weekend_after_hours = (ICP.get_param('pos_sentinel.weekend_is_after_hours') or 'False') == 'True'

        # Resolve timezone: company partner tz > user tz > UTC
        tz_name = self.env.company.partner_id.tz or self.env.user.tz or 'UTC'
        try:
            tz = pytz.timezone(tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.UTC

        now_local = pytz.UTC.localize(fields.Datetime.now()).astimezone(tz)
        current_hour = now_local.hour + now_local.minute / 60.0
        weekday = now_local.weekday()  # Monday=0, Sunday=6

        # Weekend check first (highest priority)
        if weekend_after_hours and weekday >= 5:
            return {
                'is_after_hours': True,
                'boost': boost,
                'reason': 'weekend (%s)' % ('Saturday' if weekday == 5 else 'Sunday'),
            }

        # Hours check
        if current_hour < start or current_hour >= end:
            return {
                'is_after_hours': True,
                'boost': boost,
                'reason': 'outside %.1f-%.1f (now %.2f)' % (start, end, current_hour),
            }

        return {'is_after_hours': False, 'boost': 0.0, 'reason': 'within business hours'}

    @api.model
    def _score_to_level(self, score):
        """Map a numeric score to a risk level using ICP thresholds."""
        ICP = self.env['ir.config_parameter'].sudo()
        thresholds = {}
        for level in ('low', 'medium', 'high', 'critical'):
            param = ICP.get_param(
                f'pos_sentinel.threshold_{level}',
                str(DEFAULT_THRESHOLDS[level]),
            )
            try:
                thresholds[level] = float(param)
            except (ValueError, TypeError):
                thresholds[level] = DEFAULT_THRESHOLDS[level]

        if score >= thresholds['critical']:
            return 'critical'
        elif score >= thresholds['high']:
            return 'high'
        elif score >= thresholds['medium']:
            return 'medium'
        elif score >= thresholds['low']:
            return 'low'
        return 'none'
