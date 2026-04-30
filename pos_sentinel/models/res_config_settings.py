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
