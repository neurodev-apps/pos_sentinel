# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError


class PosSentinelJustifyWizard(models.TransientModel):
    _name = 'pos.sentinel.justify.wizard'
    _description = 'Justify POS Audit Event(s)'

    event_ids = fields.Many2many(
        'pos.audit.event',
        string='Audit Events',
        required=True,
    )
    event_count = fields.Integer(
        string='Events to Justify',
        compute='_compute_summary',
    )
    is_bulk = fields.Boolean(
        compute='_compute_summary',
    )
    summary_label = fields.Char(
        string='Summary',
        compute='_compute_summary',
    )
    risk_breakdown = fields.Char(
        string='Risk Breakdown',
        compute='_compute_summary',
    )
    skipped_count = fields.Integer(
        string='Already Justified (skipped)',
    )
    justification_note = fields.Text(
        string='Justification Note',
        required=True,
        help='Explain why these events are not fraudulent. Minimum 10 characters. The same note will be applied to every selected event.',
    )

    # ── Defaults ─────────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        ctx = self.env.context

        # From multi-record list action: active_model + active_ids
        if ctx.get('active_model') == 'pos.audit.event':
            ids = ctx.get('active_ids') or []
            if not ids and ctx.get('active_id'):
                ids = [ctx['active_id']]
            if ids:
                events = self.env['pos.audit.event'].browse(ids).exists()
                pending = events.filtered(lambda e: not e.is_justified)
                defaults['event_ids'] = [(6, 0, pending.ids)]
                defaults['skipped_count'] = len(events) - len(pending)

        # From single-event form button (legacy default_event_id)
        elif ctx.get('default_event_id'):
            defaults['event_ids'] = [(6, 0, [ctx['default_event_id']])]

        return defaults

    # ── Computed summary ─────────────────────────────────────────

    @api.depends('event_ids')
    def _compute_summary(self):
        for w in self:
            count = len(w.event_ids)
            w.event_count = count
            w.is_bulk = count > 1
            if count == 0:
                w.summary_label = ''
                w.risk_breakdown = ''
                continue
            if count == 1:
                ev = w.event_ids
                w.summary_label = ev.event_label or ''
                w.risk_breakdown = dict(ev._fields['risk_level'].selection).get(ev.risk_level, ev.risk_level or '')
            else:
                w.summary_label = _('%s events selected') % count
                buckets = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'none': 0}
                for e in w.event_ids:
                    if e.risk_level in buckets:
                        buckets[e.risk_level] += 1
                parts = []
                for level in ('critical', 'high', 'medium', 'low', 'none'):
                    if buckets[level]:
                        parts.append('%s %s' % (buckets[level], level.capitalize()))
                w.risk_breakdown = ' · '.join(parts)

    # ── Actions ──────────────────────────────────────────────────

    def action_justify(self):
        self.ensure_one()
        if not self.env.user.has_group('pos_sentinel.group_pos_security_manager'):
            raise AccessError(_('Only POS Security Managers can justify audit events.'))

        note = (self.justification_note or '').strip()
        if len(note) < 10:
            raise UserError(_('Justification note must be at least 10 characters.'))

        if not self.event_ids:
            raise UserError(_('No events selected to justify.'))

        # Filter out anything that may have been justified meanwhile
        pending = self.event_ids.filtered(lambda e: not e.is_justified)
        for ev in pending:
            ev._justify(note)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Events Justified'),
                'message': _('%s event(s) marked as justified.') % len(pending),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
