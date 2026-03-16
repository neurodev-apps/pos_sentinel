# -*- coding: utf-8 -*-

import hashlib
import json
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .pos_audit_engine import get_sentinel_salt, compute_event_hash

_logger = logging.getLogger(__name__)

# Module-level token — only code within this module can know it.
# Used to gate the context-flag bypass for hash writes and integrity marks.
_SENTINEL_WRITE_TOKEN = secrets.token_hex(16)

EVENT_TYPES = [
    ('void_line', 'Line Void'),
    ('price_override', 'Price Override'),
    ('discount', 'Discount Applied'),
    ('refund', 'Refund'),
    ('cash_in', 'Cash In'),
    ('cash_out', 'Cash Out'),
    ('order_delete', 'Order Deleted'),
    ('line_qty_change', 'Quantity Changed'),
    ('payment_change', 'Payment Modified'),
    ('session_open', 'Session Opened'),
    ('session_close', 'Session Closed'),
    ('order_complete', 'Order Completed'),
    ('manual_price', 'Manual Price Entry'),
    ('negative_qty', 'Negative Quantity'),
    ('post_payment_edit', 'Post-Payment Edit'),
    ('sequence_gap', 'Sequence Gap Detected'),
    ('other', 'Other'),
]

RISK_LEVELS = [
    ('none', 'None'),
    ('low', 'Low'),
    ('medium', 'Medium'),
    ('high', 'High'),
    ('critical', 'Critical'),
]


class PosAuditEvent(models.Model):
    _name = 'pos.audit.event'
    _description = 'POS Audit Event'
    _order = 'create_date DESC, id DESC'
    _rec_name = 'display_name'

    # ── Core fields ──────────────────────────────────────────────
    event_type = fields.Selection(
        EVENT_TYPES,
        string='Event Type',
        required=True,
        readonly=True,
        index=True,
    )
    risk_level = fields.Selection(
        RISK_LEVELS,
        string='Risk Level',
        default='none',
        readonly=True,
        index=True,
    )
    risk_score = fields.Float(
        string='Risk Score',
        digits=(6, 2),
        default=0.0,
        readonly=True,
    )

    # ── POS context ──────────────────────────────────────────────
    pos_session_id = fields.Many2one(
        'pos.session',
        string='POS Session',
        readonly=True,
        index=True,
        ondelete='set null',
    )
    pos_order_id = fields.Many2one(
        'pos.order',
        string='POS Order',
        readonly=True,
        index=True,
        ondelete='set null',
    )
    pos_config_id = fields.Many2one(
        'pos.config',
        string='POS Config',
        readonly=True,
        index=True,
        ondelete='set null',
    )
    user_id = fields.Many2one(
        'res.users',
        string='User',
        required=True,
        readonly=True,
        index=True,
        ondelete='restrict',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
        readonly=True,
        ondelete='set null',
    )

    # ── Event data ───────────────────────────────────────────────
    details = fields.Text(
        string='Event Details',
        readonly=True,
        help='JSON with event-specific data (old/new values, amounts, etc.)',
    )
    amount = fields.Monetary(
        string='Amount',
        currency_field='currency_id',
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        readonly=True,
        ondelete='restrict',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product',
        readonly=True,
        ondelete='set null',
    )

    # ── Integrity ────────────────────────────────────────────────
    hash = fields.Char(
        string='Integrity Hash',
        size=64,
        readonly=True,
        copy=False,
        index=True,
        help='SHA-256 hash for tamper detection.',
    )
    is_tampered = fields.Boolean(
        string='Tampered',
        default=False,
        readonly=True,
        help='Set to True by the integrity verification cron if the hash does not match.',
    )

    # ── Multi-company ────────────────────────────────────────────
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        readonly=True,
        default=lambda self: self.env.company,
        index=True,
    )

    # ── Display ──────────────────────────────────────────────────
    display_name = fields.Char(
        compute='_compute_display_name',
        store=True,
    )

    @api.depends('event_type', 'create_date', 'user_id')
    def _compute_display_name(self):
        type_map = dict(EVENT_TYPES)
        for rec in self:
            date_str = rec.create_date.strftime('%Y-%m-%d %H:%M') if rec.create_date else ''
            user_name = rec.user_id.name or ''
            rec.display_name = f"[{type_map.get(rec.event_type, '')}] {user_name} — {date_str}"

    # ── Immutability: triple-layer protection ────────────────────

    def unlink(self):
        """Prevent deletion of audit events."""
        raise UserError(_(
            'POS audit events cannot be deleted. '
            'They are immutable for security and compliance purposes.'
        ))

    def copy(self, default=None):
        """Prevent duplication of audit events."""
        raise UserError(_(
            'POS audit events cannot be duplicated. '
            'Each entry is unique and immutable.'
        ))

    def write(self, vals):
        """Prevent modification of audit events.

        The only allowed write operations use a module-level secret token
        that external code cannot know without importing this module's
        private variable:
        - Setting the integrity hash immediately after creation.
        - Marking a record as tampered by the integrity cron.
        """
        ctx = self.env.context
        token = ctx.get('_sentinel_write_token')
        if token == _SENTINEL_WRITE_TOKEN:
            allowed_keys = set(vals.keys())
            if allowed_keys == {'hash'} or allowed_keys == {'is_tampered'}:
                return super().write(vals)
        raise UserError(_(
            'POS audit events cannot be modified. '
            'They are immutable for security and compliance purposes.'
        ))

    # ── Hash recomputation ───────────────────────────────────────

    def _recompute_hash(self):
        """Recompute SHA-256 hash for a single event record."""
        self.ensure_one()
        return compute_event_hash(
            self.env,
            user_id=self.user_id.id,
            event_type=self.event_type,
            pos_session_id=self.pos_session_id.id or 0,
            pos_order_id=self.pos_order_id.id or 0,
            create_date=self.create_date,
            details=self.details or '',
        )

    # ── Internal write helpers ───────────────────────────────────

    def _write_hash(self, hash_value):
        """Write hash using the module-level token. Internal use only."""
        return self.with_context(
            _sentinel_write_token=_SENTINEL_WRITE_TOKEN,
        ).write({'hash': hash_value})

    def _mark_tampered(self):
        """Mark records as tampered using the module-level token. Internal use only."""
        return self.with_context(
            _sentinel_write_token=_SENTINEL_WRITE_TOKEN,
        ).write({'is_tampered': True})

    # ── Event creation API ───────────────────────────────────────

    @api.model
    def create_event(self, event_type, vals):
        """Public API to create an immutable audit event.

        Args:
            event_type: One of the EVENT_TYPES selection keys.
            vals: dict with optional keys:
                - pos_session_id, pos_order_id, pos_config_id
                - employee_id, product_id
                - amount, currency_id
                - details (dict, will be JSON-serialized)
                - risk_score, risk_level

        Returns:
            The created pos.audit.event record.
        """
        details_raw = vals.get('details', {})
        if isinstance(details_raw, dict):
            details_json = json.dumps(details_raw, ensure_ascii=False, default=str)
        else:
            details_json = str(details_raw)

        # Always use current user — never trust user_id from caller
        user_id = self.env.uid
        session_id = vals.get('pos_session_id', False)
        order_id = vals.get('pos_order_id', False)

        create_vals = {
            'event_type': event_type,
            'user_id': user_id,
            'pos_session_id': session_id,
            'pos_order_id': order_id,
            'pos_config_id': vals.get('pos_config_id', False),
            'employee_id': vals.get('employee_id', False),
            'product_id': vals.get('product_id', False),
            'amount': vals.get('amount', 0.0),
            'currency_id': vals.get('currency_id', self.env.company.currency_id.id),
            'details': details_json,
            'company_id': self.env.company.id,
        }

        # ── Compute risk score via Neuro-Scoring Engine ──────────
        if 'risk_score' in vals and 'risk_level' in vals:
            create_vals['risk_score'] = vals['risk_score']
            create_vals['risk_level'] = vals['risk_level']
        else:
            try:
                scoring = self.env['pos.scoring.engine'].compute_risk(
                    event_type, {
                        'amount': vals.get('amount', 0.0),
                        'user_id': user_id,
                        'pos_session_id': session_id,
                    }
                )
                create_vals['risk_score'] = scoring['risk_score']
                create_vals['risk_level'] = scoring['risk_level']
            except Exception as e:
                _logger.warning("POS Sentinel: scoring failed, defaulting to none: %s", e)
                create_vals['risk_score'] = 0.0
                create_vals['risk_level'] = 'none'

        create_vals['hash'] = ''

        try:
            record = self.sudo().create(create_vals)

            # Compute hash using the real create_date from the DB
            audit_hash = compute_event_hash(
                self.env,
                user_id=user_id,
                event_type=event_type,
                pos_session_id=session_id or 0,
                pos_order_id=order_id or 0,
                create_date=record.create_date,
                details=details_json,
            )
            record._write_hash(audit_hash)
            return record
        except Exception as e:
            _logger.critical("POS Sentinel: failed to create audit event: %s", e)
            return self.env['pos.audit.event']

    # ── Batch event logging (called from OWL frontend) ─────────

    @api.model
    def log_events_batch(self, events):
        """Receive a batch of events from the POS frontend Shadow Logger.

        Called via orm.call() from the sentinel_service.js.

        Args:
            events: list of dicts with event_type and optional data.

        Returns:
            dict with 'created' count.
        """
        created = 0
        for event_data in events:
            event_type = event_data.get('event_type')
            if not event_type:
                continue
            try:
                self.create_event(event_type, event_data)
                created += 1
            except Exception as e:
                _logger.warning(
                    "POS Sentinel: failed to log event %s: %s",
                    event_type, e,
                )
        return {'created': created}

    # ── Integrity verification cron ──────────────────────────────

    @api.model
    def _cron_verify_integrity(self):
        """Scheduled action: verify hash integrity of recent audit events.

        Processes events from the last 7 days in batches of 1000 using
        raw SQL for performance. Marks tampered records and persists
        results for the dashboard.
        """
        date_from = fields.Datetime.now() - timedelta(days=7)
        salt = get_sentinel_salt(self.env)

        self.env.cr.execute("""
            SELECT id, user_id, event_type, pos_session_id, pos_order_id,
                   create_date, details, hash
            FROM pos_audit_event
            WHERE create_date >= %s
            ORDER BY id ASC
        """, (date_from,))

        total = 0
        tampered_ids = []
        BATCH = 1000

        while True:
            rows = self.env.cr.fetchmany(BATCH)
            if not rows:
                break
            total += len(rows)
            for row in rows:
                (log_id, user_id, event_type, session_id, order_id,
                 create_date, details, stored_hash) = row

                date_str = (
                    create_date.strftime('%Y-%m-%d %H:%M:%S')
                    if create_date else ''
                )
                hash_input = (
                    f"{user_id}|{event_type}|{session_id or 0}|{order_id or 0}"
                    f"|{date_str}|{details or ''}|{salt}"
                )
                expected = hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
                if stored_hash != expected:
                    tampered_ids.append(log_id)
                    _logger.warning(
                        "POS SENTINEL INTEGRITY ALERT: pos.audit.event id=%s "
                        "hash mismatch (stored=%s, expected=%s)",
                        log_id, stored_hash, expected,
                    )

        # Mark tampered records
        if tampered_ids:
            tampered_records = self.sudo().browse(tampered_ids)
            tampered_records._mark_tampered()

        # Persist results for dashboard
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(
            'pos_sentinel.last_integrity_check',
            fields.Datetime.to_string(fields.Datetime.now()),
        )
        ICP.set_param(
            'pos_sentinel.last_tampered_count',
            str(len(tampered_ids)),
        )

        _logger.info(
            "POS Sentinel: integrity check completed — %d events checked, "
            "%d tampered detected",
            total, len(tampered_ids),
        )

    # ── Cleanup cron ─────────────────────────────────────────────

    @api.model
    def _cron_cleanup_old_events(self):
        """Retention report: count audit events older than the configured period.

        Uses the system parameter ``pos_sentinel.retention_days`` (default 365).
        Events are NOT deleted — this cron only reports the count for awareness.
        Immutability is preserved.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            retention_days = int(ICP.get_param('pos_sentinel.retention_days', '365'))
        except (ValueError, TypeError):
            retention_days = 365
        cutoff = fields.Datetime.now() - timedelta(days=retention_days)

        self.env.cr.execute("""
            SELECT COUNT(*)
            FROM pos_audit_event
            WHERE create_date < %s
        """, (cutoff,))
        count = self.env.cr.fetchone()[0]

        if count:
            _logger.info(
                "POS Sentinel: %d audit events older than %d days "
                "(retention policy — records preserved for compliance)",
                count, retention_days,
            )
