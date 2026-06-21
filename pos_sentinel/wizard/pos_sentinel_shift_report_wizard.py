# -*- coding: utf-8 -*-

import base64
import io
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError

from ..models.pos_audit_event import EVENT_TYPES, RISK_LEVELS

_logger = logging.getLogger(__name__)

EVENT_TYPE_LABELS = dict(EVENT_TYPES)
RISK_LEVEL_LABELS = dict(RISK_LEVELS)

RISK_ORDER = ['critical', 'high', 'medium', 'low', 'none']


class PosSentinelShiftReportWizard(models.TransientModel):
    _name = 'pos.sentinel.shift.report.wizard'
    _description = 'POS Sentinel Shift Report'

    pos_session_id = fields.Many2one(
        'pos.session',
        string='Session',
        required=True,
    )
    report_format = fields.Selection(
        [('pdf', 'PDF'), ('xlsx', 'Excel (XLSX)')],
        string='Format',
        default='pdf',
        required=True,
    )

    # ── Data builder ─────────────────────────────────────────────

    def _get_shift_data(self):
        self.ensure_one()
        session = self.pos_session_id
        # PS-CR-04: the SQL below filters only by session id with raw cr.execute,
        # bypassing record rules. Enforce company scope explicitly so a session
        # of another company can never be reported here (cross-company leak).
        if session.company_id and session.company_id not in self.env.companies:
            raise AccessError(_('You do not have access to this POS session.'))
        cr = self.env.cr

        # Per-cashier breakdown
        cr.execute("""
            SELECT
                COALESCE(rp.name, ru.login)          AS uname,
                COUNT(*)                              AS total_events,
                COUNT(*) FILTER (WHERE pae.event_type = 'void_line')      AS voids,
                COUNT(*) FILTER (WHERE pae.event_type = 'discount')       AS discounts,
                COUNT(*) FILTER (WHERE pae.event_type = 'refund')         AS refunds,
                COUNT(*) FILTER (WHERE pae.event_type = 'price_override') AS price_overrides,
                COUNT(*) FILTER (WHERE pae.event_type = 'cash_out')       AS cash_outs,
                COALESCE(SUM(pae.risk_score), 0)     AS total_score,
                MAX(pae.risk_level)                   AS worst_risk
            FROM pos_audit_event pae
            JOIN res_users ru ON pae.user_id = ru.id
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            WHERE pae.pos_session_id = %s
            GROUP BY rp.name, ru.login
            ORDER BY total_score DESC
        """, [session.id])
        by_cashier = cr.dictfetchall()

        # Top 10 riskiest events
        cr.execute("""
            SELECT
                pae.create_date,
                pae.event_type,
                COALESCE(rp.name, ru.login) AS uname,
                pae.risk_level,
                pae.risk_score,
                pae.amount,
                pae.is_justified
            FROM pos_audit_event pae
            JOIN res_users ru ON pae.user_id = ru.id
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            WHERE pae.pos_session_id = %s
              AND pae.risk_level IN ('critical', 'high', 'medium')
            ORDER BY pae.risk_score DESC
            LIMIT 10
        """, [session.id])
        top_events = cr.dictfetchall()

        # Risk distribution
        cr.execute("""
            SELECT risk_level, COUNT(*) AS cnt
            FROM pos_audit_event
            WHERE pos_session_id = %s
            GROUP BY risk_level
        """, [session.id])
        by_risk_raw = {r['risk_level']: r['cnt'] for r in cr.dictfetchall()}
        by_risk = [(RISK_LEVEL_LABELS.get(rl, rl), by_risk_raw[rl])
                   for rl in RISK_ORDER if rl in by_risk_raw]

        # Totals
        cr.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(risk_score), 0) AS total_score,
                   COUNT(*) FILTER (WHERE is_tampered = TRUE) AS tampered
            FROM pos_audit_event
            WHERE pos_session_id = %s
        """, [session.id])
        totals = cr.dictfetchone()

        duration = ''
        if session.start_at and session.stop_at:
            delta = session.stop_at - session.start_at
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m = rem // 60
            duration = f'{h}h {m:02d}m'

        return {
            'session': session,
            'duration': duration,
            'total_events': totals['total'],
            'total_score': round(float(totals['total_score']), 1),
            'tampered': totals['tampered'],
            'by_risk': by_risk,
            'by_cashier': by_cashier,
            'top_events': top_events,
            'event_label': EVENT_TYPE_LABELS,
            'risk_label': RISK_LEVEL_LABELS,
            'company': self.env.company,
        }

    # ── PDF ──────────────────────────────────────────────────────

    def action_generate_pdf(self):
        self.ensure_one()
        data = self._get_shift_data()
        return self.env.ref(
            'pos_sentinel.action_report_sentinel_shift_pdf'
        ).report_action(self, data={'report_data': {
            'session_name': data['session'].name,
            'session_state': data['session'].state,
            'config_name': data['session'].config_id.name,
            'start_at': fields.Datetime.to_string(data['session'].start_at),
            'stop_at': fields.Datetime.to_string(data['session'].stop_at) if data['session'].stop_at else '',
            'duration': data['duration'],
            'total_events': data['total_events'],
            'total_score': data['total_score'],
            'tampered': data['tampered'],
            'by_risk': data['by_risk'],
            'by_cashier': [
                {
                    'uname': r['uname'],
                    'total_events': r['total_events'],
                    'voids': r['voids'],
                    'discounts': r['discounts'],
                    'refunds': r['refunds'],
                    'price_overrides': r['price_overrides'],
                    'cash_outs': r['cash_outs'],
                    'total_score': round(float(r['total_score']), 1),
                    'worst_risk': RISK_LEVEL_LABELS.get(r['worst_risk'], ''),
                }
                for r in data['by_cashier']
            ],
            'top_events': [
                {
                    'create_date': fields.Datetime.to_string(r['create_date']),
                    'event_type': EVENT_TYPE_LABELS.get(r['event_type'], r['event_type']),
                    'uname': r['uname'],
                    'risk_level': RISK_LEVEL_LABELS.get(r['risk_level'], ''),
                    'risk_score': round(float(r['risk_score']), 1),
                    'amount': round(float(r['amount'] or 0), 2),
                    'is_justified': r['is_justified'],
                }
                for r in data['top_events']
            ],
            'company_name': data['company'].name,
        }})

    # ── Excel ────────────────────────────────────────────────────

    def action_generate_xlsx(self):
        self.ensure_one()
        data = self._get_shift_data()

        try:
            import xlsxwriter
        except ImportError:
            raise UserError(_('xlsxwriter is required. Install with: pip install xlsxwriter'))

        output = io.BytesIO()
        # PS-CR-03: disable formula/URL auto-conversion so DB values starting
        # with = + - @ are written as plain text, never executable formulas.
        wb = xlsxwriter.Workbook(output, {
            'in_memory': True,
            'strings_to_formulas': False,
            'strings_to_urls': False,
        })

        hdr = wb.add_format({'bold': True, 'bg_color': '#312E81', 'font_color': 'white', 'border': 1})
        title_fmt = wb.add_format({'bold': True, 'font_size': 14})
        sub_fmt = wb.add_format({'bold': True, 'font_size': 11, 'bottom': 1})
        num_fmt = wb.add_format({'num_format': '#,##0'})
        score_fmt = wb.add_format({'num_format': '#,##0.0'})
        red_fmt = wb.add_format({'bg_color': '#F8D7DA', 'border': 1})
        yellow_fmt = wb.add_format({'bg_color': '#FFF3CD', 'border': 1})

        session = data['session']

        # ── Sheet 1: Summary ──────────────────────────────────────
        ws = wb.add_worksheet('Summary')
        ws.set_column('A:A', 28)
        ws.set_column('B:B', 18)

        ws.write(0, 0, 'POS Sentinel — Shift Report', title_fmt)
        ws.write(1, 0, f"Session: {session.name}")
        ws.write(2, 0, f"POS Config: {session.config_id.name}")
        ws.write(3, 0, f"Start: {fields.Datetime.to_string(session.start_at)}")
        ws.write(4, 0, f"End: {fields.Datetime.to_string(session.stop_at) if session.stop_at else 'Open'}")
        ws.write(5, 0, f"Duration: {data['duration']}")
        ws.write(6, 0, f"Company: {data['company'].name}")

        row = 8
        ws.write(row, 0, 'Totals', sub_fmt); ws.write(row, 1, '', sub_fmt); row += 1
        ws.write(row, 0, 'Total Audit Events'); ws.write(row, 1, data['total_events'], num_fmt); row += 1
        ws.write(row, 0, 'Total Risk Score'); ws.write(row, 1, data['total_score'], score_fmt); row += 1
        ws.write(row, 0, 'Tampered Records'); ws.write(row, 1, data['tampered'], num_fmt); row += 1

        row += 1
        ws.write(row, 0, 'By Risk Level', sub_fmt); ws.write(row, 1, 'Count', sub_fmt); row += 1
        for label, count in data['by_risk']:
            ws.write(row, 0, label); ws.write(row, 1, count, num_fmt); row += 1

        # ── Sheet 2: By Cashier ───────────────────────────────────
        ws2 = wb.add_worksheet('By Cashier')
        cashier_headers = ['Cashier', 'Events', 'Voids', 'Discounts', 'Refunds',
                           'Price Overrides', 'Cash Outs', 'Risk Score', 'Worst Risk']
        widths2 = [25, 10, 10, 12, 12, 16, 12, 14, 14]
        for i, (h, w) in enumerate(zip(cashier_headers, widths2)):
            ws2.set_column(i, i, w)
            ws2.write(0, i, h, hdr)

        for ri, r in enumerate(data['by_cashier'], 1):
            fmt = red_fmt if r['worst_risk'] in ('critical', 'high') else (yellow_fmt if r['worst_risk'] == 'medium' else None)
            ws2.write(ri, 0, r['uname'], fmt)
            ws2.write(ri, 1, r['total_events'], fmt or num_fmt)
            ws2.write(ri, 2, r['voids'], fmt or num_fmt)
            ws2.write(ri, 3, r['discounts'], fmt or num_fmt)
            ws2.write(ri, 4, r['refunds'], fmt or num_fmt)
            ws2.write(ri, 5, r['price_overrides'], fmt or num_fmt)
            ws2.write(ri, 6, r['cash_outs'], fmt or num_fmt)
            ws2.write(ri, 7, round(float(r['total_score']), 1), fmt or score_fmt)
            ws2.write(ri, 8, RISK_LEVEL_LABELS.get(r['worst_risk'], ''), fmt)

        # ── Sheet 3: Top Events ───────────────────────────────────
        ws3 = wb.add_worksheet('Top Events')
        event_headers = ['Date', 'Event Type', 'Cashier', 'Risk Level', 'Risk Score', 'Amount', 'Justified']
        widths3 = [20, 20, 25, 14, 12, 14, 10]
        for i, (h, w) in enumerate(zip(event_headers, widths3)):
            ws3.set_column(i, i, w)
            ws3.write(0, i, h, hdr)

        for ri, r in enumerate(data['top_events'], 1):
            fmt = red_fmt if r['risk_level'] in ('critical', 'high') else yellow_fmt
            ws3.write(ri, 0, fields.Datetime.to_string(r['create_date']), fmt)
            ws3.write(ri, 1, EVENT_TYPE_LABELS.get(r['event_type'], r['event_type']), fmt)
            ws3.write(ri, 2, r['uname'], fmt)
            ws3.write(ri, 3, RISK_LEVEL_LABELS.get(r['risk_level'], ''), fmt)
            ws3.write(ri, 4, round(float(r['risk_score']), 1), fmt or score_fmt)
            ws3.write(ri, 5, round(float(r['amount'] or 0), 2), fmt or num_fmt)
            ws3.write(ri, 6, 'Yes' if r['is_justified'] else '', fmt)

        wb.close()
        output.seek(0)
        filename = f"shift_report_{(session.name or 'session').replace('/', '_')}.xlsx"
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(output.read()),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_generate_report(self):
        self.ensure_one()
        if self.report_format == 'pdf':
            return self.action_generate_pdf()
        return self.action_generate_xlsx()
