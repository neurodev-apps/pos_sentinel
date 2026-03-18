# -*- coding: utf-8 -*-

import logging
from odoo import models

_logger = logging.getLogger(__name__)


class PosSession(models.Model):
    _inherit = 'pos.session'

    def open_frontend_cb(self):
        """Capture session_open event when POS frontend opens."""
        result = super().open_frontend_cb()
        try:
            self.env['pos.audit.event'].sudo().create_event('session_open', {
                'pos_session_id': self.id,
                'pos_config_id': self.config_id.id,
                'details': {
                    'session_name': self.name,
                    'config_name': self.config_id.name,
                    'cashier': self.env.user.name,
                },
            })
        except Exception as e:
            _logger.warning("POS Sentinel: failed to log session_open: %s", e)
        return result

    def try_cash_in_out(self, _type, amount, reason, extras):
        """Capture cash_in / cash_out events with actual amount and direction."""
        result = super().try_cash_in_out(_type, amount, reason, extras)
        try:
            event_type = 'cash_in' if _type == 'in' else 'cash_out'
            self.env['pos.audit.event'].sudo().create_event(event_type, {
                'pos_session_id': self.id,
                'pos_config_id': self.config_id.id,
                'amount': abs(amount),
                'details': {
                    'type': _type,
                    'amount': amount,
                    'reason': reason or '',
                    'session_name': self.name,
                },
            })
        except Exception as e:
            _logger.warning("POS Sentinel: failed to log %s: %s", event_type, e)
        return result
