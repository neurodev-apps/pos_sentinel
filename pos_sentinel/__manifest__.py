# -*- coding: utf-8 -*-
{
    'name': 'POS Sentinel - Behavioral Fraud Detection',
    'version': '17.0.1.6.0',
    'category': 'Point of Sale',
    'summary': 'Real-time behavioral fraud detection and immutable audit trail for Odoo POS',
    'description': """
POS Sentinel — Behavioral Fraud Detection for Odoo POS
=======================================================

Detects suspicious patterns in Point of Sale operations using a behavioral
scoring engine. Every POS event is captured in an **immutable, SHA-256 signed
audit log** that cannot be modified, deleted, or duplicated.

Key Features
------------
* **Shadow Logger**: Captures POS events in real time without affecting cashier workflow.
* **Neuro-Scoring Engine**: Calculates risk scores based on configurable behavioral rules.
* **Immutable Audit Trail**: SHA-256 integrity hashing with dynamic salt — tamper-evident.
* **Real-time Alerts**: Automatic notifications when risk thresholds are exceeded.
* **Compliance Dashboard**: OWL 2 dashboard with charts and drill-down analytics.
* **Multi-company**: Full multi-company support with company-scoped security rules.

Monitored Events
----------------
* Line voids and modifications after payment
* Manual price overrides and excessive discounts
* Refunds and returns
* Cash movements (in/out)
* Session open/close anomalies
* Order deletions and sequence gaps

Security
--------
* Triple-layer immutability: ORM overrides + ACL + record rules
* SHA-256 integrity hashing with cryptographically-secure dynamic salt
* Automated integrity verification via scheduled action
* Separation of duties: POS Auditor (read-only) vs Security Manager (config)
    """,
    'author': 'NeuroDev',
    'website': 'https://neurodev.cl',
    'support': 'contacto@neurodev.cl',
    'license': 'OPL-1',
    'price': 249.00,
    'currency': 'USD',
    'uninstall_hook': 'uninstall_hook',
    'depends': [
        'point_of_sale',
        'hr',
    ],
    'data': [
        'security/pos_sentinel_security.xml',
        'security/ir.model.access.csv',
        'data/pos_sentinel_data.xml',
        'data/pos_sentinel_scoring_rules.xml',
        'data/pos_sentinel_alert_template.xml',
        'views/pos_sentinel_justify_wizard_views.xml',
        'views/pos_audit_event_views.xml',
        'views/pos_scoring_rule_views.xml',
        'views/pos_sentinel_report_wizard_views.xml',
        'views/pos_sentinel_menuitem.xml',
        'views/res_config_settings_views.xml',
        'report/pos_sentinel_report.xml',
        'report/pos_sentinel_report_template.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_sentinel/static/src/js/sentinel_service.js',
            'pos_sentinel/static/src/js/shadow_logger.js',
        ],
        'web.assets_backend': [
            'pos_sentinel/static/src/js/dashboard.js',
            'pos_sentinel/static/src/xml/dashboard.xml',
            'pos_sentinel/static/src/css/dashboard.css',
        ],
    },
    'images': [
        'static/description/banner.png',
    ],
    'installable': True,
    'auto_install': False,
    'application': True,
    'sequence': 200,
}
