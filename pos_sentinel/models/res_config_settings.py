# -*- coding: utf-8 -*-

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── POS Sentinel — Real-time Critical Alerts ────────────────
    pos_sentinel_alert_enabled = fields.Boolean(
        string='Enable Real-time Alerts',
        config_parameter='pos_sentinel.alert_enabled',
        help='Send notifications when high-risk POS events are detected.',
    )
    pos_sentinel_alert_threshold = fields.Selection(
        [
            ('high', 'High and Critical'),
            ('critical', 'Critical only'),
        ],
        string='Alert Threshold',
        config_parameter='pos_sentinel.alert_threshold',
        default='critical',
        help='Minimum risk level that triggers an alert.',
    )
    pos_sentinel_alert_email_to = fields.Char(
        string='Alert Recipients (Email)',
        config_parameter='pos_sentinel.alert_email_to',
        help='Comma-separated list of email addresses that receive alerts.',
    )
    pos_sentinel_alert_webhook_url = fields.Char(
        string='Webhook URL',
        config_parameter='pos_sentinel.alert_webhook_url',
        help='Optional. URL to receive alerts as JSON POST '
             '(Slack, Telegram bot, Discord, Twilio for WhatsApp/SMS).',
    )
    pos_sentinel_alert_webhook_format = fields.Selection(
        [
            ('generic', 'Generic JSON'),
            ('slack', 'Slack'),
            ('telegram', 'Telegram'),
            ('discord', 'Discord'),
        ],
        string='Webhook Format',
        config_parameter='pos_sentinel.alert_webhook_format',
        default='generic',
    )

    # ── POS Sentinel — Negative / Low Margin Detection ──────────
    pos_sentinel_margin_evaluate_enabled = fields.Boolean(
        string='Detect Margin Anomalies',
        config_parameter='pos_sentinel.margin_evaluate_enabled',
        default=True,
        help='When enabled, every POS order line is evaluated. Sales below '
             'cost generate a Critical "Negative Margin" event; sales below '
             'the configured margin threshold generate a Medium "Low Margin" event.',
    )
    pos_sentinel_margin_threshold_pct = fields.Float(
        string='Low-Margin Threshold (%)',
        config_parameter='pos_sentinel.margin_threshold_pct',
        default=5.0,
        help='Margin percentage below which a "Low Margin" event is created. '
             'Sales below cost (negative margin) are always flagged regardless of this value.',
    )
    pos_sentinel_margin_skip_zero_cost = fields.Boolean(
        string='Skip Products Without Cost',
        config_parameter='pos_sentinel.margin_skip_zero_cost',
        default=True,
        help='When enabled, products with standard_price = 0 are excluded '
             'from margin evaluation (avoids false positives on services or '
             'unconfigured products).',
    )

    # ── POS Sentinel — After-Hours Activity Detection ───────────
    pos_sentinel_after_hours_enabled = fields.Boolean(
        string='Detect After-Hours Activity',
        config_parameter='pos_sentinel.after_hours_enabled',
        default=True,
        help='When enabled, every event created outside business hours '
             '(or on weekends if configured) is flagged with is_after_hours '
             'and receives an automatic risk score boost.',
    )
    pos_sentinel_business_hours_start = fields.Float(
        string='Business Hours Start',
        config_parameter='pos_sentinel.business_hours_start',
        default=8.0,
        help='Start of business hours in 24h format (e.g. 8.0 = 08:00, 9.5 = 09:30). '
             'Events outside this window are flagged as after-hours.',
    )
    pos_sentinel_business_hours_end = fields.Float(
        string='Business Hours End',
        config_parameter='pos_sentinel.business_hours_end',
        default=22.0,
        help='End of business hours in 24h format (e.g. 22.0 = 22:00, 21.5 = 21:30).',
    )
    pos_sentinel_weekend_is_after_hours = fields.Boolean(
        string='Weekends are After-Hours',
        config_parameter='pos_sentinel.weekend_is_after_hours',
        default=False,
        help='When enabled, all events on Saturday and Sunday are flagged as '
             'after-hours regardless of the time of day.',
    )
    pos_sentinel_after_hours_boost = fields.Float(
        string='After-Hours Score Boost',
        config_parameter='pos_sentinel.after_hours_boost',
        default=25.0,
        help='Points added to the risk score when an event occurs outside '
             'business hours. Higher boost means after-hours events escalate '
             'risk levels faster (e.g. a Low event becomes Medium).',
    )

    def action_pos_sentinel_test_alert(self):
        """Send a test alert with sample data to verify the configuration."""
        self.ensure_one()
        self.env['pos.audit.event']._send_test_alert()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'POS Sentinel',
                'message': 'Test alert sent. Check your email and webhook.',
                'type': 'success',
                'sticky': False,
            },
        }
