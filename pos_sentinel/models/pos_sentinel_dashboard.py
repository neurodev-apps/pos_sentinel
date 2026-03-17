# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PosSentinelDashboard(models.AbstractModel):
    _name = 'pos.sentinel.dashboard'
    _description = 'POS Sentinel Dashboard Data Provider'

    @api.model
    def get_dashboard_data(self, date_from=None, date_to=None):
        """Single RPC call to provide all dashboard data.

        Uses raw SQL for performance on large datasets.
        All queries are company-scoped to prevent multi-company data leaks.

        Args:
            date_from: ISO date string (default: 30 days ago)
            date_to: ISO date string (default: now)

        Returns:
            dict with keys: summary, by_type, by_risk, by_day, by_user,
                           top_products, integrity, recent_critical
        """
        now = fields.Datetime.now()
        if date_from:
            dt_from = fields.Datetime.from_string(date_from)
        else:
            dt_from = now - timedelta(days=30)
        if date_to:
            dt_to = fields.Datetime.from_string(date_to)
        else:
            dt_to = now

        company_ids = self.env.companies.ids
        if not company_ids:
            return {
                'summary': {}, 'by_type': [], 'by_risk': [],
                'by_day': [], 'by_user': [], 'top_products': [],
                'integrity': self._get_integrity_status(),
                'recent_critical': [],
            }

        return {
            'summary': self._get_summary(dt_from, dt_to, company_ids),
            'by_type': self._get_events_by_type(dt_from, dt_to, company_ids),
            'by_risk': self._get_events_by_risk(dt_from, dt_to, company_ids),
            'by_day': self._get_events_by_day(dt_from, dt_to, company_ids),
            'by_user': self._get_events_by_user(dt_from, dt_to, company_ids),
            'top_products': self._get_top_products(dt_from, dt_to, company_ids),
            'integrity': self._get_integrity_status(),
            'recent_critical': self._get_recent_critical(dt_from, dt_to, company_ids),
        }

    def _get_summary(self, dt_from, dt_to, company_ids):
        """Summary cards: total events, by risk level, tampered count."""
        self.env.cr.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE risk_level = 'critical') AS critical,
                COUNT(*) FILTER (WHERE risk_level = 'high') AS high,
                COUNT(*) FILTER (WHERE risk_level = 'medium') AS medium,
                COUNT(*) FILTER (WHERE risk_level = 'low') AS low,
                COUNT(*) FILTER (WHERE risk_level = 'none') AS none_level,
                COUNT(*) FILTER (WHERE is_tampered = TRUE) AS tampered,
                COALESCE(AVG(risk_score), 0) AS avg_score
            FROM pos_audit_event
            WHERE create_date BETWEEN %s AND %s
              AND company_id IN %s
        """, (dt_from, dt_to, tuple(company_ids)))
        return self.env.cr.dictfetchone()

    def _get_events_by_type(self, dt_from, dt_to, company_ids):
        """Events grouped by event_type for doughnut chart."""
        self.env.cr.execute("""
            SELECT event_type, COUNT(*) AS count
            FROM pos_audit_event
            WHERE create_date BETWEEN %s AND %s
              AND company_id IN %s
            GROUP BY event_type
            ORDER BY count DESC
        """, (dt_from, dt_to, tuple(company_ids)))
        return self.env.cr.dictfetchall()

    def _get_events_by_risk(self, dt_from, dt_to, company_ids):
        """Events grouped by risk_level for horizontal bar chart."""
        self.env.cr.execute("""
            SELECT risk_level, COUNT(*) AS count,
                   COALESCE(AVG(risk_score), 0) AS avg_score
            FROM pos_audit_event
            WHERE create_date BETWEEN %s AND %s
              AND company_id IN %s
            GROUP BY risk_level
            ORDER BY
                CASE risk_level
                    WHEN 'critical' THEN 1
                    WHEN 'high' THEN 2
                    WHEN 'medium' THEN 3
                    WHEN 'low' THEN 4
                    ELSE 5
                END
        """, (dt_from, dt_to, tuple(company_ids)))
        return self.env.cr.dictfetchall()

    def _get_events_by_day(self, dt_from, dt_to, company_ids):
        """Events per day for line chart."""
        self.env.cr.execute("""
            SELECT
                to_char(create_date::date, 'YYYY-MM-DD') AS day,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE risk_level IN ('high', 'critical')) AS risky
            FROM pos_audit_event
            WHERE create_date BETWEEN %s AND %s
              AND company_id IN %s
            GROUP BY create_date::date
            ORDER BY create_date::date
        """, (dt_from, dt_to, tuple(company_ids)))
        return self.env.cr.dictfetchall()

    def _get_events_by_user(self, dt_from, dt_to, company_ids):
        """Top 10 users by total risk score."""
        self.env.cr.execute("""
            SELECT
                ru.login AS user_login,
                COALESCE(rp.name, ru.login) AS user_name,
                COUNT(*) AS event_count,
                COALESCE(SUM(pae.risk_score), 0) AS total_score,
                COALESCE(AVG(pae.risk_score), 0) AS avg_score
            FROM pos_audit_event pae
            JOIN res_users ru ON pae.user_id = ru.id
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            WHERE pae.create_date BETWEEN %s AND %s
              AND pae.company_id IN %s
            GROUP BY ru.id, ru.login, rp.name
            ORDER BY total_score DESC
            LIMIT 10
        """, (dt_from, dt_to, tuple(company_ids)))
        return self.env.cr.dictfetchall()

    def _get_top_products(self, dt_from, dt_to, company_ids):
        """Top 10 products involved in risky events."""
        lang = self.env.lang or 'en_US'
        self.env.cr.execute("""
            SELECT
                COALESCE(
                    pt.name->>%s,
                    (SELECT value FROM jsonb_each_text(pt.name) LIMIT 1),
                    'Unknown'
                ) AS product_name,
                COUNT(*) AS event_count,
                COALESCE(SUM(pae.risk_score), 0) AS total_score
            FROM pos_audit_event pae
            JOIN product_product pp ON pae.product_id = pp.id
            JOIN product_template pt ON pp.product_tmpl_id = pt.id
            WHERE pae.create_date BETWEEN %s AND %s
              AND pae.company_id IN %s
              AND pae.product_id IS NOT NULL
              AND pae.risk_score > 0
            GROUP BY pt.name
            ORDER BY total_score DESC
            LIMIT 10
        """, (lang, dt_from, dt_to, tuple(company_ids)))
        return self.env.cr.dictfetchall()

    def _get_integrity_status(self):
        """Last integrity check results from ICP."""
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            tampered_count = int(ICP.get_param('pos_sentinel.last_tampered_count', '0'))
        except (ValueError, TypeError):
            tampered_count = 0
        return {
            'last_check': ICP.get_param('pos_sentinel.last_integrity_check', ''),
            'tampered_count': tampered_count,
        }

    def _get_recent_critical(self, dt_from, dt_to, company_ids):
        """Last 20 critical/high risk events for the activity feed."""
        self.env.cr.execute("""
            SELECT
                pae.id,
                pae.event_type,
                pae.risk_level,
                pae.risk_score,
                pae.amount,
                pae.create_date,
                COALESCE(rp.name, ru.login) AS user_name,
                pae.details
            FROM pos_audit_event pae
            JOIN res_users ru ON pae.user_id = ru.id
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            WHERE pae.create_date BETWEEN %s AND %s
              AND pae.company_id IN %s
              AND pae.risk_level IN ('high', 'critical')
            ORDER BY pae.create_date DESC
            LIMIT 20
        """, (dt_from, dt_to, tuple(company_ids)))
        rows = self.env.cr.dictfetchall()
        for row in rows:
            if row.get('create_date'):
                row['create_date'] = fields.Datetime.to_string(row['create_date'])
        return rows
