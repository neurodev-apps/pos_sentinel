# -*- coding: utf-8 -*-

import hmac
import json
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .pos_audit_engine import (
    get_sentinel_salt, compute_event_hash, compute_event_hash_v2,
    HASH_VERSION_HMAC_CHAIN, _CHAIN_LOCK_KEY,
)

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
    ('negative_margin', 'Negative Margin'),
    ('low_margin', 'Low Margin'),
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
    _rec_name = 'event_label'

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

    # ── Margin metrics (populated for negative_margin / low_margin) ──
    cost_at_sale = fields.Monetary(
        string='Cost at Sale',
        currency_field='currency_id',
        readonly=True,
        help='Standard cost of the product at the moment of the sale.',
    )
    selling_price = fields.Monetary(
        string='Selling Price (net)',
        currency_field='currency_id',
        readonly=True,
        help='Effective unit price after discount, before tax.',
    )
    margin_amount = fields.Monetary(
        string='Margin Amount',
        currency_field='currency_id',
        readonly=True,
        help='Total margin in money for this line (selling - cost) * qty.',
    )
    margin_pct = fields.Float(
        string='Margin %',
        digits=(6, 2),
        readonly=True,
        help='Percentage margin relative to the selling price.',
    )
    quantity = fields.Float(
        string='Quantity',
        digits=(12, 3),
        readonly=True,
        help='Quantity sold for this line (used for margin events).',
    )

    # ── Integrity ────────────────────────────────────────────────
    hash = fields.Char(
        string='Integrity Hash',
        size=64,
        readonly=True,
        copy=False,
        index=True,
        help='HMAC-SHA256 integrity hash over the full payload, chained to the '
             'previous event (v2). Tamper- and deletion-evident.',
    )
    previous_hash = fields.Char(
        string='Previous Hash',
        size=64,
        readonly=True,
        copy=False,
        help='Integrity hash of the preceding event. Links the chain so that '
             'deleting or reordering an intermediate record is detected (PS-CR-02).',
    )
    hash_version = fields.Integer(
        string='Hash Version',
        readonly=True,
        copy=False,
        help='Integrity algorithm. Empty/1 = legacy salted SHA-256; '
             '2 = chained HMAC-SHA256 over the full payload.',
    )
    is_tampered = fields.Boolean(
        string='Tampered',
        default=False,
        readonly=True,
        help='Set to True by the integrity verification cron if the hash does not match.',
    )

    # ── After-hours flag ─────────────────────────────────────────
    is_after_hours = fields.Boolean(
        string='After Hours',
        default=False,
        readonly=True,
        index=True,
        help='True if the event occurred outside the configured business hours '
             '(or on a weekend, when weekend-as-after-hours is enabled).',
    )

    # ── Forgiveness ──────────────────────────────────────────────
    is_justified = fields.Boolean(
        string='Justified',
        default=False,
        copy=False,
        index=True,
        help='Marked as justified by a Security Manager. Does not alter the hash.',
    )
    justification_note = fields.Text(
        string='Justification Note',
        copy=False,
        readonly=True,
    )
    justified_by_id = fields.Many2one(
        'res.users',
        string='Justified By',
        copy=False,
        readonly=True,
        ondelete='restrict',
    )
    justified_date = fields.Datetime(
        string='Justified On',
        copy=False,
        readonly=True,
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
    event_label = fields.Char(
        string='Event Label',
        compute='_compute_event_label',
        store=True,
    )

    @api.depends('event_type', 'create_date', 'user_id')
    def _compute_event_label(self):
        type_map = dict(EVENT_TYPES)
        for rec in self:
            date_str = rec.create_date.strftime('%Y-%m-%d %H:%M') if rec.create_date else ''
            user_name = rec.user_id.name or ''
            rec.event_label = f"[{type_map.get(rec.event_type, '')}] {user_name} — {date_str}"

    # ── Immutability: triple-layer protection ────────────────────

    def unlink(self):
        """Prevent deletion of audit events.

        An unlink on an empty recordset is a legitimate no-op: Odoo core and
        other modules call ``model.browse().unlink()`` generically. Only block
        the operation when there is actually something to delete (PS-CR-07).
        """
        if not self:
            return True
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
            _JUSTIFICATION_FIELDS = {
                'is_justified', 'justification_note',
                'justified_by_id', 'justified_date',
            }
            if (allowed_keys == {'hash'}
                    or allowed_keys == {'is_tampered'}
                    or allowed_keys <= _JUSTIFICATION_FIELDS):
                return super().write(vals)
        raise UserError(_(
            'POS audit events cannot be modified. '
            'They are immutable for security and compliance purposes.'
        ))

    # ── Hash recomputation ───────────────────────────────────────

    def _recompute_hash(self):
        """Recompute the integrity hash using the algorithm in ``hash_version``.

        v2 reproduces the chained HMAC over the full payload; legacy records
        (empty/1) reproduce the original salted SHA-256, so events created
        before the upgrade keep verifying. The chain link itself is checked by
        the integrity cron, not here.
        """
        self.ensure_one()
        if (self.hash_version or 1) >= 2:
            create_date_str = (
                self.create_date.strftime('%Y-%m-%d %H:%M:%S')
                if self.create_date else ''
            )
            return compute_event_hash_v2(self.env, {
                'user_id': self.user_id.id,
                'event_type': self.event_type,
                'pos_session_id': self.pos_session_id.id or 0,
                'pos_order_id': self.pos_order_id.id or 0,
                'company_id': self.company_id.id if self.company_id else False,
                'amount': self.amount,
                'risk_level': self.risk_level,
                'create_date': create_date_str,
                'details': self.details or '',
                'previous_hash': self.previous_hash or '',
            })
        # Legacy v1 salted SHA-256
        return compute_event_hash(
            self.env,
            user_id=self.user_id.id,
            event_type=self.event_type,
            pos_session_id=self.pos_session_id.id or 0,
            pos_order_id=self.pos_order_id.id or 0,
            create_date=self.create_date,
            details=self.details or '',
        )

    # ── Forgiveness ──────────────────────────────────────────────

    def _justify(self, note):
        """Mark this event as justified without altering the integrity hash."""
        self.ensure_one()
        self.with_context(_sentinel_write_token=_SENTINEL_WRITE_TOKEN).write({
            'is_justified': True,
            'justification_note': note,
            'justified_by_id': self.env.user.id,
            'justified_date': fields.Datetime.now(),
        })

    def action_open_justify_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Justify Event',
            'res_model': 'pos.sentinel.justify.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_event_id': self.id},
        }

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

        # Sanitize Many2one IDs: in Odoo 18 the POS frontend sends local
        # string IDs like "pos.order_3" before backend sync. We only accept
        # real integer IDs; otherwise we store the local id in details for
        # forensic tracing and set the FK to False.
        def _safe_int_id(v):
            if isinstance(v, bool):
                return False
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
            return False

        session_id_raw = vals.get('pos_session_id', False)
        order_id_raw = vals.get('pos_order_id', False)
        config_id_raw = vals.get('pos_config_id', False)
        product_id_raw = vals.get('product_id', False)
        employee_id_raw = vals.get('employee_id', False)

        session_id = _safe_int_id(session_id_raw)
        order_id = _safe_int_id(order_id_raw)
        config_id = _safe_int_id(config_id_raw)
        product_id = _safe_int_id(product_id_raw)
        employee_id = _safe_int_id(employee_id_raw)

        # Preserve original local IDs in details for forensic auditing
        if isinstance(order_id_raw, str) and not order_id:
            try:
                details_dict = json.loads(details_json) if details_json else {}
            except Exception:
                details_dict = {}
            details_dict['_local_pos_order_id'] = order_id_raw
            details_json = json.dumps(details_dict, ensure_ascii=False, default=str)

        # PS-CR-06: company of the AFFECTED record, not the user's active company.
        # Backend callers may pass company_id explicitly; otherwise derive it
        # from the order / session / config; fall back to env.company.
        company_id = vals.get('company_id')
        if not company_id and order_id:
            order = self.env['pos.order'].browse(order_id)
            company_id = order.company_id.id if order.exists() else False
        if not company_id and session_id:
            session = self.env['pos.session'].browse(session_id)
            company_id = session.company_id.id if session.exists() else False
        if not company_id and config_id:
            config = self.env['pos.config'].browse(config_id)
            company_id = config.company_id.id if config.exists() else False
        if not company_id:
            company_id = self.env.company.id

        create_vals = {
            'event_type': event_type,
            'user_id': user_id,
            'pos_session_id': session_id,
            'pos_order_id': order_id,
            'pos_config_id': config_id,
            'employee_id': employee_id,
            'product_id': product_id,
            'amount': vals.get('amount', 0.0),
            'currency_id': vals.get('currency_id', self.env.company.currency_id.id),
            'details': details_json,
            'company_id': company_id,
        }

        # Margin metrics — populated for negative_margin / low_margin events
        if 'cost_at_sale' in vals:
            create_vals['cost_at_sale'] = vals.get('cost_at_sale', 0.0)
        if 'selling_price' in vals:
            create_vals['selling_price'] = vals.get('selling_price', 0.0)
        if 'margin_amount' in vals:
            create_vals['margin_amount'] = vals.get('margin_amount', 0.0)
        if 'margin_pct' in vals:
            create_vals['margin_pct'] = vals.get('margin_pct', 0.0)
        if 'quantity' in vals:
            create_vals['quantity'] = vals.get('quantity', 0.0)

        # ── After-hours detection (global) ─────────────────────
        try:
            after_hours_info = self.env['pos.scoring.engine']._evaluate_after_hours_global()
            if after_hours_info['is_after_hours']:
                create_vals['is_after_hours'] = True
                # Boost the risk score and re-map the level
                boost = after_hours_info['boost']
                if boost > 0 and create_vals.get('risk_score', 0) > 0:
                    new_score = min(create_vals['risk_score'] + boost, 100.0)
                    create_vals['risk_score'] = round(new_score, 2)
                    create_vals['risk_level'] = self.env['pos.scoring.engine']._score_to_level(new_score)
        except Exception as e:
            _logger.warning("POS Sentinel: after-hours evaluation failed: %s", e)

        # ── Compute risk score via Neuro-Scoring Engine ──────────
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
            # PS-CR-02: serialise the chain and read the tail hash atomically so
            # concurrent transactions never link to the same predecessor.
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', (_CHAIN_LOCK_KEY,))
            self.env.cr.execute('SELECT hash FROM pos_audit_event ORDER BY id DESC LIMIT 1')
            row = self.env.cr.fetchone()
            previous_hash = (row[0] if row else '') or ''
            create_vals['previous_hash'] = previous_hash
            create_vals['hash_version'] = HASH_VERSION_HMAC_CHAIN

            record = self.sudo().create(create_vals)

            # PS-CR-01: HMAC-SHA256 over the full payload using the real
            # create_date and the stored company/risk values.
            create_date_str = (
                record.create_date.strftime('%Y-%m-%d %H:%M:%S')
                if record.create_date else ''
            )
            audit_hash = compute_event_hash_v2(self.env, {
                'user_id': user_id,
                'event_type': event_type,
                'pos_session_id': session_id or 0,
                'pos_order_id': order_id or 0,
                'company_id': record.company_id.id if record.company_id else False,
                'amount': create_vals.get('amount') or 0.0,
                'risk_level': create_vals.get('risk_level') or '',
                'create_date': create_date_str,
                'details': details_json,
                'previous_hash': previous_hash,
            })
            record._write_hash(audit_hash)
            # Persist the signed hash immediately so the record is durable and
            # raw-SQL readers (the integrity cron) always see the final value.
            record.flush_recordset(['hash', 'previous_hash', 'hash_version'])

            # Dispatch real-time alert for high/critical events
            if record.risk_level in ('high', 'critical'):
                try:
                    record._maybe_dispatch_alert()
                except Exception as e:
                    _logger.warning("POS Sentinel: alert dispatch failed: %s", e)

            return record
        except Exception as e:
            _logger.critical("POS Sentinel: failed to create audit event: %s", e)
            return self.env['pos.audit.event']

    # ── Real-time Alerts (v1.5) ──────────────────────────────────

    def _maybe_dispatch_alert(self):
        """Dispatch alert via email and/or webhook if configured.

        Called from create_event when risk_level is 'high' or 'critical'.
        Reads ir.config_parameter for settings.

        Errors are logged but never propagated — alerting is a best-effort
        side-effect that must never break event creation.
        """
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()

        if ICP.get_param('pos_sentinel.alert_enabled') != 'True':
            return

        threshold = ICP.get_param('pos_sentinel.alert_threshold', 'critical')
        if threshold == 'critical' and self.risk_level != 'critical':
            return  # only critical alerts allowed

        # Email
        email_to = ICP.get_param('pos_sentinel.alert_email_to', '')
        if email_to:
            try:
                self._send_alert_email(email_to)
            except Exception as e:
                _logger.warning("POS Sentinel: email alert failed: %s", e)

        # Webhook
        webhook_url = ICP.get_param('pos_sentinel.alert_webhook_url', '')
        if webhook_url:
            try:
                webhook_format = ICP.get_param(
                    'pos_sentinel.alert_webhook_format', 'generic',
                )
                self._send_alert_webhook(webhook_url, webhook_format)
            except Exception as e:
                _logger.warning("POS Sentinel: webhook alert failed: %s", e)

    def _send_alert_email(self, email_to):
        """Send the alert email using the mail.template."""
        self.ensure_one()
        template = self.env.ref(
            'pos_sentinel.email_template_pos_sentinel_alert',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("POS Sentinel: email template not found")
            return
        template.send_mail(
            self.id,
            force_send=True,
            email_values={'email_to': email_to},
        )

    @api.model
    def _is_safe_webhook_url(self, url):
        """PS-CR-05: allow only https URLs to public hosts (anti-SSRF).

        Rejects non-https schemes and any host that resolves to a private,
        loopback, link-local, reserved or cloud-metadata address. Fail-closed:
        if the host cannot be resolved/validated, the webhook is not sent.
        """
        import ipaddress
        import socket
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url or '')
            if parsed.scheme != 'https' or not parsed.hostname:
                return False
            for res in socket.getaddrinfo(parsed.hostname, None):
                ip = ipaddress.ip_address(res[4][0])
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                    return False
            return True
        except Exception:
            return False

    def _send_alert_webhook(self, url, fmt='generic'):
        """POST a JSON payload to the configured webhook.

        Supported formats: generic, slack, telegram, discord. The payload
        is shaped so that the most common bots/tools accept it as-is.
        """
        self.ensure_one()
        try:
            import requests
        except ImportError:
            _logger.warning("POS Sentinel: 'requests' library not available")
            return

        # PS-CR-05: validate the destination before any network call.
        if not self._is_safe_webhook_url(url):
            _logger.warning(
                "POS Sentinel: webhook URL rejected (must be https to a public host)"
            )
            return

        type_label = dict(self._fields['event_type'].selection).get(
            self.event_type, self.event_type,
        )
        emoji = '🚨' if self.risk_level == 'critical' else '⚠️'
        title = f"{emoji} POS Sentinel — {self.risk_level.upper()}"
        message = (
            f"*{type_label}* by *{self.user_id.name}* "
            f"at *{self.pos_config_id.name or 'POS'}*\n"
            f"Amount: {self.amount or 0:.2f} · Score: {self.risk_score}/100"
        )

        if fmt == 'slack':
            payload = {
                'text': title,
                'blocks': [
                    {'type': 'header', 'text': {'type': 'plain_text', 'text': title}},
                    {'type': 'section', 'text': {'type': 'mrkdwn', 'text': message}},
                ],
            }
        elif fmt == 'telegram':
            # Telegram bots expect chat_id; we send a generic text payload
            payload = {
                'text': f"{title}\n{message}",
                'parse_mode': 'Markdown',
            }
        elif fmt == 'discord':
            payload = {
                'content': title,
                'embeds': [{
                    'title': type_label,
                    'description': message,
                    'color': 15158332 if self.risk_level == 'critical' else 16489728,
                }],
            }
        else:  # generic
            payload = {
                'event_id': self.id,
                'event_type': self.event_type,
                'risk_level': self.risk_level,
                'risk_score': self.risk_score,
                'user': self.user_id.name,
                'pos_config': self.pos_config_id.name,
                'amount': self.amount,
                'currency': self.currency_id.name,
                'create_date': fields.Datetime.to_string(self.create_date),
                'company': self.company_id.name,
                'message': f"{title} — {message}",
            }

        try:
            # PS-CR-05: don't follow redirects (could bounce to an internal host).
            response = requests.post(url, json=payload, timeout=5, allow_redirects=False)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # PS-MD-01: log only the host — the path carries the webhook secret.
            from urllib.parse import urlparse
            _logger.warning(
                "POS Sentinel: webhook POST failed (host=%s): %s",
                urlparse(url).netloc, e,
            )

    @api.model
    def _send_test_alert(self):
        """Trigger a test alert with a dummy event for configuration testing."""
        # Build a transient (non-persisted) record-like object for the test
        ICP = self.env['ir.config_parameter'].sudo()
        email_to = ICP.get_param('pos_sentinel.alert_email_to', '')
        webhook_url = ICP.get_param('pos_sentinel.alert_webhook_url', '')

        if not email_to and not webhook_url:
            from odoo.exceptions import UserError
            raise UserError(
                "Configure at least one alert channel "
                "(email or webhook) before sending a test."
            )

        # Find or create a sample critical event for the test
        sample = self.search([('risk_level', '=', 'critical')], limit=1)
        if not sample:
            sample = self.search([('risk_level', '=', 'high')], limit=1)
        if not sample:
            sample = self.search([], limit=1)
        if not sample:
            from odoo.exceptions import UserError
            raise UserError(
                "No POS audit events exist yet. Create at least one event "
                "before sending a test alert."
            )

        if email_to:
            sample._send_alert_email(email_to)
        if webhook_url:
            fmt = ICP.get_param(
                'pos_sentinel.alert_webhook_format', 'generic',
            )
            sample._send_alert_webhook(webhook_url, fmt)
        return True

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
        # PS-MD-02: bound the public RPC — cap batch size and per-event payload
        # to prevent log inflation / storage DoS from the POS client.
        MAX_BATCH = 200
        MAX_DETAILS = 16384  # 16 KB per event
        allowed_types = {t[0] for t in EVENT_TYPES}
        if not isinstance(events, (list, tuple)):
            return {'created': 0}
        created = 0
        for event_data in events[:MAX_BATCH]:
            if not isinstance(event_data, dict):
                continue
            event_type = event_data.get('event_type')
            if not event_type or event_type not in allowed_types:
                continue
            details = event_data.get('details')
            if isinstance(details, str) and len(details) > MAX_DETAILS:
                event_data = dict(event_data, details=details[:MAX_DETAILS])
            elif isinstance(details, dict):
                try:
                    if len(json.dumps(details, default=str)) > MAX_DETAILS:
                        event_data = dict(event_data, details={'_truncated': True})
                except Exception:
                    event_data = dict(event_data, details={})
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
        # PS-MD-03: window configurable; 0 (default) = verify the FULL history.
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            window_days = int(ICP.get_param('pos_sentinel.integrity_window_days', '0'))
        except (ValueError, TypeError):
            window_days = 0

        where = ''
        params = ()
        if window_days > 0:
            where = 'WHERE create_date >= %s'
            params = (fields.Datetime.now() - timedelta(days=window_days),)

        # No company filter: the hash chain is global, so the whole table is
        # walked in id order to verify chain continuity (PS-CR-02).
        self.env.cr.execute("""
            SELECT id, user_id, event_type, pos_session_id, pos_order_id,
                   create_date, details, hash, company_id, amount, risk_level,
                   previous_hash, hash_version
            FROM pos_audit_event
            """ + where + """
            ORDER BY id ASC
        """, params)

        total = 0
        tampered_ids = []
        BATCH = 1000
        prev_stored_hash = ''  # hash of the preceding row — for the chain check

        while True:
            rows = self.env.cr.fetchmany(BATCH)
            if not rows:
                break
            total += len(rows)
            for row in rows:
                (log_id, user_id, event_type, session_id, order_id,
                 create_date, details, stored_hash, company_id, amount,
                 risk_level, previous_hash, hash_version) = row
                create_date_str = create_date.strftime('%Y-%m-%d %H:%M:%S') if create_date else ''
                tampered = False

                if (hash_version or 1) >= 2:
                    expected = compute_event_hash_v2(self.env, {
                        'user_id': user_id,
                        'event_type': event_type,
                        'pos_session_id': session_id or 0,
                        'pos_order_id': order_id or 0,
                        'company_id': company_id or False,
                        'amount': amount or 0.0,
                        'risk_level': risk_level or '',
                        'create_date': create_date_str,
                        'details': details or '',
                        'previous_hash': previous_hash,
                    })
                    # Chain link: previous_hash must equal the prior row's hash.
                    if (previous_hash or '') != (prev_stored_hash or ''):
                        tampered = True
                else:
                    expected = compute_event_hash(
                        self.env,
                        user_id=user_id,
                        event_type=event_type,
                        pos_session_id=session_id or 0,
                        pos_order_id=order_id or 0,
                        create_date=create_date,
                        details=details or '',
                    )

                if not hmac.compare_digest(stored_hash or '', expected):
                    tampered = True

                if tampered:
                    tampered_ids.append(log_id)
                    _logger.warning(
                        "POS SENTINEL INTEGRITY ALERT: pos.audit.event id=%s "
                        "hash/chain mismatch", log_id,
                    )

                prev_stored_hash = stored_hash

        # Mark tampered records
        if tampered_ids:
            self.sudo().browse(tampered_ids)._mark_tampered()

        # Persist results for dashboard
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

        company_ids = self.env.companies.ids
        if not company_ids:
            return

        self.env.cr.execute("""
            SELECT COUNT(*)
            FROM pos_audit_event
            WHERE create_date < %s
              AND company_id IN %s
        """, (cutoff, tuple(company_ids)))
        count = self.env.cr.fetchone()[0]

        if count:
            _logger.info(
                "POS Sentinel: %d audit events older than %d days "
                "(retention policy — records preserved for compliance)",
                count, retention_days,
            )
