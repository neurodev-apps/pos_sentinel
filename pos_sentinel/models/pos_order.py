# -*- coding: utf-8 -*-

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """Hook into pos.order to evaluate margin events post-creation.

    For each line of every newly-synced POS order, we compute the effective
    margin (selling price after discount, before tax — vs product cost) and
    raise either a 'negative_margin' or 'low_margin' audit event when below
    the configured threshold.
    """
    _inherit = 'pos.order'

    def _pos_sentinel_margin_settings(self):
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            threshold_pct = float(ICP.get_param('pos_sentinel.margin_threshold_pct') or 5.0)
        except (TypeError, ValueError):
            threshold_pct = 5.0
        skip_zero_cost = (ICP.get_param('pos_sentinel.margin_skip_zero_cost') or 'True') == 'True'
        evaluate_enabled = (ICP.get_param('pos_sentinel.margin_evaluate_enabled') or 'True') == 'True'
        return {
            'threshold_pct': threshold_pct,
            'skip_zero_cost': skip_zero_cost,
            'evaluate_enabled': evaluate_enabled,
        }

    def _pos_sentinel_log_margin_for_line(self, line, settings):
        """Evaluate one POS order line and log a margin event if applicable."""
        product = line.product_id
        if not product:
            return False

        cost = product.standard_price or 0.0
        if settings['skip_zero_cost'] and cost <= 0:
            return False

        qty = line.qty or 0.0
        if qty == 0:
            return False

        # price_subtotal: total amount of the line BEFORE tax, AFTER discount.
        line_subtotal = line.price_subtotal or 0.0
        if line_subtotal <= 0 and cost > 0:
            # Free or zero-priced line with positive cost — full loss
            margin_pct = -100.0
            price_unit_net = 0.0
        else:
            price_unit_net = line_subtotal / qty if qty else 0.0
            if price_unit_net <= 0:
                margin_pct = -100.0
            else:
                margin_pct = ((price_unit_net - cost) / price_unit_net) * 100.0

        margin_amount_unit = price_unit_net - cost
        margin_amount_total = margin_amount_unit * qty

        if margin_pct < 0:
            event_type = 'negative_margin'
        elif margin_pct < settings['threshold_pct']:
            event_type = 'low_margin'
        else:
            return False

        order = line.order_id
        details = {
            'order_name': order.name or '',
            'product_name': product.display_name or '',
            'product_default_code': product.default_code or '',
            'qty': qty,
            'cost_unit': round(cost, 4),
            'selling_price_unit_net': round(price_unit_net, 4),
            'margin_unit': round(margin_amount_unit, 4),
            'margin_total': round(margin_amount_total, 4),
            'margin_pct': round(margin_pct, 2),
            'threshold_pct': settings['threshold_pct'],
            'has_discount': bool(getattr(line, 'discount', 0.0)),
            'discount_pct': getattr(line, 'discount', 0.0) or 0.0,
        }

        try:
            self.env['pos.audit.event'].sudo().create_event(event_type, {
                'pos_session_id': order.session_id.id if order.session_id else False,
                'pos_order_id': order.id,
                'pos_config_id': order.config_id.id if order.config_id else False,
                'product_id': product.id,
                'amount': margin_amount_total,
                'currency_id': order.currency_id.id if order.currency_id else False,
                'details': details,
                'cost_at_sale': cost,
                'selling_price': price_unit_net,
                'margin_amount': margin_amount_total,
                'margin_pct': margin_pct,
                'quantity': qty,
            })
            return True
        except Exception as e:
            _logger.warning(
                "POS Sentinel: failed to log margin event for order %s line %s: %s",
                order.name, line.id, e,
            )
            return False

    def _pos_sentinel_evaluate_margins(self):
        """Evaluate margins for every line of every order in self."""
        if not self:
            return
        settings = self._pos_sentinel_margin_settings()
        if not settings['evaluate_enabled']:
            return
        for order in self:
            for line in order.lines:
                self._pos_sentinel_log_margin_for_line(line, settings)

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        try:
            orders._pos_sentinel_evaluate_margins()
        except Exception as e:
            # Never break order creation because of margin evaluation
            _logger.warning("POS Sentinel: margin evaluation skipped: %s", e)
        return orders
