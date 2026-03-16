# -*- coding: utf-8 -*-

import base64
import io
import json
import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

EVENT_TYPE_LABELS = {
    'void_line': 'Line Void',
    'price_override': 'Price Override',
    'discount': 'Discount Applied',
    'refund': 'Refund',
    'cash_in': 'Cash In',
    'cash_out': 'Cash Out',
    'order_delete': 'Order Deleted',
    'line_qty_change': 'Quantity Changed',
    'payment_change': 'Payment Modified',
    'session_open': 'Session Opened',
    'session_close': 'Session Closed',
    'order_complete': 'Order Completed',
    'manual_price': 'Manual Price Entry',
    'negative_qty': 'Negative Quantity',
    'post_payment_edit': 'Post-Payment Edit',
    'sequence_gap': 'Sequence Gap',
    'other': 'Other',
}

RISK_LEVEL_LABELS = {
    'none': 'None',
    'low': 'Low',
    'medium': 'Medium',
    'high': 'High',
    'critical': 'Critical',
}


class PosSentinelReportWizard(models.TransientModel):
    _name = 'pos.sentinel.report.wizard'
    _description = 'POS Sentinel Report Wizard'

    date_from = fields.Datetime(
        string='From',
        required=True,
        default=lambda self: fields.Datetime.now() - timedelta(days=30),
    )
    date_to = fields.Datetime(
        string='To',
        required=True,
        default=lambda self: fields.Datetime.now(),
    )
    risk_levels = fields.Selection(
        [
            ('all', 'All Levels'),
            ('risky', 'Medium + High + Critical'),
            ('high_critical', 'High + Critical Only'),
            ('critical', 'Critical Only'),
        ],
        string='Risk Level Filter',
        default='all',
        required=True,
    )
    report_format = fields.Selection(
        [
            ('pdf', 'PDF'),
            ('xlsx', 'Excel (XLSX)'),
        ],
        string='Format',
        default='pdf',
        required=True,
    )

    def _get_risk_domain(self):
        """Convert risk_levels selection to domain filter."""
        mapping = {
            'all': [],
            'risky': [('risk_level', 'in', ('medium', 'high', 'critical'))],
            'high_critical': [('risk_level', 'in', ('high', 'critical'))],
            'critical': [('risk_level', '=', 'critical')],
        }
        return mapping.get(self.risk_levels, [])

    def _get_events(self):
        """Fetch events matching the wizard filters, company-scoped."""
        domain = [
            ('create_date', '>=', self.date_from),
            ('create_date', '<=', self.date_to),
            ('company_id', 'in', self.env.companies.ids),
        ] + self._get_risk_domain()
        return self.env['pos.audit.event'].sudo().search(
            domain, order='create_date DESC', limit=10000,
        )

    def _get_report_data(self):
        """Build report data dict using SQL for performance."""
        self.ensure_one()
        company_ids = self.env.companies.ids

        # Use SQL for aggregations instead of iterating ORM records
        risk_domain = self._get_risk_domain()
        risk_sql = ""
        params = [self.date_from, self.date_to, tuple(company_ids)]

        if risk_domain:
            risk_filter = risk_domain[0]
            if risk_filter[1] == 'in':
                risk_sql = " AND risk_level IN %s"
                params.append(tuple(risk_filter[2]))
            elif risk_filter[1] == '=':
                risk_sql = " AND risk_level = %s"
                params.append(risk_filter[2])

        base_where = f"WHERE create_date >= %s AND create_date <= %s AND company_id IN %s{risk_sql}"
        qual_where = f"WHERE pae.create_date >= %s AND pae.create_date <= %s AND pae.company_id IN %s{risk_sql.replace('risk_level', 'pae.risk_level')}"

        # Summary by risk
        self.env.cr.execute(f"""
            SELECT risk_level, COUNT(*) as cnt
            FROM pos_audit_event {base_where}
            GROUP BY risk_level
        """, params)
        by_risk_raw = {r['risk_level']: r['cnt'] for r in self.env.cr.dictfetchall()}

        # Summary by type
        self.env.cr.execute(f"""
            SELECT event_type, COUNT(*) as cnt
            FROM pos_audit_event {base_where}
            GROUP BY event_type ORDER BY cnt DESC
        """, params)
        by_type_raw = self.env.cr.dictfetchall()

        # Summary by user
        self.env.cr.execute(f"""
            SELECT COALESCE(rp.name, ru.login) as uname,
                   COUNT(*) as cnt,
                   COALESCE(SUM(pae.risk_score), 0) as total_score
            FROM pos_audit_event pae
            JOIN res_users ru ON pae.user_id = ru.id
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            {qual_where}
            GROUP BY rp.name, ru.login
            ORDER BY total_score DESC
        """, params)
        by_user_raw = self.env.cr.dictfetchall()

        # Total and tampered count
        self.env.cr.execute(f"""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE is_tampered = TRUE) as tampered
            FROM pos_audit_event {base_where}
        """, params)
        totals = self.env.cr.dictfetchone()

        # For the detail sheet (Excel only), fetch events via ORM with limit
        events = self._get_events()

        risk_order = ['critical', 'high', 'medium', 'low', 'none']
        by_risk = []
        for rl in risk_order:
            if rl in by_risk_raw:
                by_risk.append((RISK_LEVEL_LABELS.get(rl, rl), by_risk_raw[rl]))

        return {
            'date_from': self.date_from,
            'date_to': self.date_to,
            'risk_filter': dict(self._fields['risk_levels'].selection).get(self.risk_levels),
            'total': totals['total'],
            'tampered': totals['tampered'],
            'by_risk': by_risk,
            'by_type': [(EVENT_TYPE_LABELS.get(r['event_type'], r['event_type']), r['cnt'])
                        for r in by_type_raw],
            'by_user': [(r['uname'], r['cnt'], round(float(r['total_score']), 1))
                        for r in by_user_raw],
            'events': events,
            'company': self.env.company,
        }

    # ── PDF ───────────────────────────────────────────────────────

    def action_generate_pdf(self):
        """Generate and download PDF report."""
        self.ensure_one()
        data = self._get_report_data()
        return self.env.ref(
            'pos_sentinel.action_report_sentinel_pdf'
        ).report_action(self, data={'report_data': {
            'date_from': fields.Datetime.to_string(data['date_from']),
            'date_to': fields.Datetime.to_string(data['date_to']),
            'risk_filter': data['risk_filter'],
            'total': data['total'],
            'tampered': data['tampered'],
            'by_risk': data['by_risk'],
            'by_type': data['by_type'],
            'by_user': data['by_user'],
            'company_name': data['company'].name,
        }})

    # ── Excel ─────────────────────────────────────────────────────

    def action_generate_xlsx(self):
        """Generate and download Excel report."""
        self.ensure_one()
        data = self._get_report_data()

        try:
            import xlsxwriter
        except ImportError:
            raise UserError(_(
                'xlsxwriter is required for Excel export. '
                'Install it with: pip install xlsxwriter'
            ))

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        # Formats
        header_fmt = wb.add_format({
            'bold': True, 'bg_color': '#875A7B', 'font_color': 'white',
            'border': 1, 'text_wrap': True, 'valign': 'vcenter',
        })
        title_fmt = wb.add_format({
            'bold': True, 'font_size': 14,
        })
        subtitle_fmt = wb.add_format({
            'bold': True, 'font_size': 11, 'bottom': 1,
        })
        num_fmt = wb.add_format({'num_format': '#,##0'})
        score_fmt = wb.add_format({'num_format': '#,##0.0'})
        date_fmt = wb.add_format({'num_format': 'yyyy-mm-dd hh:mm:ss'})
        risk_critical = wb.add_format({'bg_color': '#F8D7DA', 'border': 1})
        risk_high = wb.add_format({'bg_color': '#FFF3CD', 'border': 1})

        # ── Sheet 1: Summary ─────────────────────────────────────
        ws = wb.add_worksheet('Summary')
        ws.set_column('A:A', 25)
        ws.set_column('B:B', 15)

        ws.write(0, 0, 'POS Sentinel Report', title_fmt)
        ws.write(1, 0, f"Company: {data['company'].name}")
        ws.write(2, 0, f"Period: {data['date_from'].strftime('%Y-%m-%d')} to {data['date_to'].strftime('%Y-%m-%d')}")
        ws.write(3, 0, f"Filter: {data['risk_filter']}")

        row = 5
        ws.write(row, 0, 'Summary', subtitle_fmt)
        ws.write(row, 1, '', subtitle_fmt)
        row += 1
        ws.write(row, 0, 'Total Events')
        ws.write(row, 1, data['total'], num_fmt)
        row += 1
        ws.write(row, 0, 'Tampered Records')
        ws.write(row, 1, data['tampered'], num_fmt)

        row += 2
        ws.write(row, 0, 'By Risk Level', subtitle_fmt)
        ws.write(row, 1, 'Count', subtitle_fmt)
        row += 1
        for label, count in data['by_risk']:
            ws.write(row, 0, label)
            ws.write(row, 1, count, num_fmt)
            row += 1

        row += 1
        ws.write(row, 0, 'By Event Type', subtitle_fmt)
        ws.write(row, 1, 'Count', subtitle_fmt)
        row += 1
        for label, count in data['by_type']:
            ws.write(row, 0, label)
            ws.write(row, 1, count, num_fmt)
            row += 1

        # ── Sheet 2: Events Detail ───────────────────────────────
        ws2 = wb.add_worksheet('Events')
        headers = ['Date', 'Event Type', 'User', 'Risk Level', 'Risk Score',
                   'Amount', 'Product', 'POS Config', 'Order', 'Hash', 'Tampered']
        widths = [20, 18, 20, 12, 10, 14, 25, 18, 15, 66, 10]
        for i, (h, w) in enumerate(zip(headers, widths)):
            ws2.set_column(i, i, w)
            ws2.write(0, i, h, header_fmt)

        for row_idx, ev in enumerate(data['events'], 1):
            fmt = None
            if ev.risk_level == 'critical':
                fmt = risk_critical
            elif ev.risk_level == 'high':
                fmt = risk_high

            ws2.write(row_idx, 0, ev.create_date.strftime('%Y-%m-%d %H:%M:%S') if ev.create_date else '', fmt or date_fmt)
            ws2.write(row_idx, 1, EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type), fmt)
            ws2.write(row_idx, 2, ev.user_id.name or '', fmt)
            ws2.write(row_idx, 3, RISK_LEVEL_LABELS.get(ev.risk_level, ''), fmt)
            ws2.write(row_idx, 4, ev.risk_score or 0, fmt or score_fmt)
            ws2.write(row_idx, 5, ev.amount or 0, fmt or num_fmt)
            ws2.write(row_idx, 6, ev.product_id.display_name or '', fmt)
            ws2.write(row_idx, 7, ev.pos_config_id.name or '', fmt)
            ws2.write(row_idx, 8, ev.pos_order_id.name or '', fmt)
            ws2.write(row_idx, 9, ev.hash or '', fmt)
            ws2.write(row_idx, 10, 'YES' if ev.is_tampered else '', fmt)

            if row_idx >= 10000:
                break

        ws2.autofilter(0, 0, min(len(data['events']), 10000), len(headers) - 1)

        # ── Sheet 3: By User ─────────────────────────────────────
        ws3 = wb.add_worksheet('By User')
        ws3.set_column('A:A', 30)
        ws3.set_column('B:B', 12)
        ws3.set_column('C:C', 14)
        ws3.write(0, 0, 'User', header_fmt)
        ws3.write(0, 1, 'Events', header_fmt)
        ws3.write(0, 2, 'Total Score', header_fmt)

        for row_idx, (user, count, score) in enumerate(data['by_user'], 1):
            ws3.write(row_idx, 0, user)
            ws3.write(row_idx, 1, count, num_fmt)
            ws3.write(row_idx, 2, score, score_fmt)

        wb.close()

        # Save as attachment and return download action
        filename = f"pos_sentinel_report_{data['date_from'].strftime('%Y%m%d')}_{data['date_to'].strftime('%Y%m%d')}.xlsx"
        output.seek(0)
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

    # ── Dispatch ──────────────────────────────────────────────────

    def action_generate_report(self):
        """Main button action — dispatches to PDF or Excel."""
        self.ensure_one()
        if self.report_format == 'pdf':
            return self.action_generate_pdf()
        return self.action_generate_xlsx()
