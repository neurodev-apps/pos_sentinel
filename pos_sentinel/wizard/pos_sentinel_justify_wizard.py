# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError


class PosSentinelJustifyWizard(models.TransientModel):
    _name = 'pos.sentinel.justify.wizard'
    _description = 'Justify POS Audit Event'

    event_id = fields.Many2one(
        'pos.audit.event',
        string='Audit Event',
        required=True,
        readonly=True,
    )
    event_label = fields.Char(
        related='event_id.event_label',
        string='Event',
        readonly=True,
    )
    risk_level = fields.Selection(
        related='event_id.risk_level',
        string='Risk Level',
        readonly=True,
    )
    risk_score = fields.Float(
        related='event_id.risk_score',
        string='Risk Score',
        readonly=True,
    )
    justification_note = fields.Text(
        string='Justification Note',
        required=True,
        help='Explain why this event is not fraudulent. Minimum 10 characters.',
    )

    def action_justify(self):
        self.ensure_one()
        if not self.env.user.has_group('pos_sentinel.group_pos_security_manager'):
            raise AccessError(_('Only POS Security Managers can justify audit events.'))
        note = (self.justification_note or '').strip()
        if len(note) < 10:
            raise UserError(_('Justification note must be at least 10 characters.'))
        self.event_id._justify(note)
        return {'type': 'ir.actions.act_window_close'}
