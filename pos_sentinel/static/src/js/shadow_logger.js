/** @odoo-module */

/**
 * POS Sentinel — Shadow Logger (Odoo 17)
 *
 * Patches POS models and components to silently capture fraud-relevant events.
 *
 * Odoo 17 naming conventions:
 * - Order class (not PosOrder) from @point_of_sale/app/store/models
 * - Orderline class (not PosOrderline) from @point_of_sale/app/store/models
 * - PosStore from @point_of_sale/app/store/pos_store
 * - PaymentScreen from @point_of_sale/app/screens/payment_screen/payment_screen
 * - Methods: removeOrderline (camelCase), add_paymentline/remove_paymentline (snake_case)
 * - Orderline methods: set_quantity, set_unit_price, set_discount (snake_case)
 * - Orderline parent: this.order (not order_id)
 * - Order total: get_total_with_tax() (not priceIncl)
 * - Refund: _isRefundOrder() (not isRefund)
 * - PosStore: removeOrder (singular), closePos (not closeSession)
 *
 * Design principles:
 * - Fire-and-forget: errors never block the POS UI
 * - Zero overhead: no popups, no delays for the cashier
 * - Captures old/new values for forensic analysis
 */

import { patch } from "@web/core/utils/patch";
import { Order, Orderline } from "@point_of_sale/app/store/models";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

// ─── Helpers ─────────────────────────────────────────────────────

/** Get sentinel from global (for model patches without env) */
function sentinel() {
    return window.__posSentinel || null;
}

/** Extract POS context from a model instance (order or orderline) */
function ctx(obj) {
    try {
        // v17: Orderline uses this.order (not order_id)
        const order = obj.order || obj;
        return {
            pos_session_id: order.pos_session_id || false,
            pos_config_id: order.pos?.config?.id ?? false,
        };
    } catch {
        return { pos_session_id: false, pos_config_id: false };
    }
}

// ─── Order patches ──────────────────────────────────────────────
patch(Order.prototype, {
    /**
     * Capture: line removal (void)
     * v17: removeOrderline is camelCase
     */
    removeOrderline(line) {
        const s = sentinel();
        if (s && line) {
            try {
                s.logEvent("void_line", {
                    ...ctx(this),
                    pos_order_id: this.id || false,
                    product_id: line.product?.id || false,
                    amount: (line.price || 0) * (line.quantity || 0),
                    details: {
                        product_name: line.get_full_product_name?.() || "",
                        qty: line.quantity || 0,
                        price_unit: line.price || 0,
                        discount: line.discount || 0,
                        order_name: this.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] void_line error:", e);
            }
        }
        return super.removeOrderline(...arguments);
    },

    /**
     * Capture: payment line removal
     * v17: remove_paymentline is snake_case
     */
    remove_paymentline(line) {
        const s = sentinel();
        if (s && line) {
            try {
                s.logEvent("payment_change", {
                    ...ctx(this),
                    pos_order_id: this.id || false,
                    amount: line.amount || 0,
                    details: {
                        action: "remove_payment",
                        payment_method: line.payment_method?.name || "",
                        amount: line.amount || 0,
                        order_name: this.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] payment_change error:", e);
            }
        }
        return super.remove_paymentline(...arguments);
    },

    /**
     * Capture: payment line added
     * v17: add_paymentline is snake_case
     */
    add_paymentline(payment_method) {
        const result = super.add_paymentline(...arguments);
        const s = sentinel();
        if (s) {
            try {
                s.logEvent("payment_change", {
                    ...ctx(this),
                    pos_order_id: this.id || false,
                    amount: 0,
                    details: {
                        action: "add_payment",
                        payment_method: payment_method?.name || "",
                        order_name: this.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] payment_change error:", e);
            }
        }
        return result;
    },
});

// ─── Orderline patches ──────────────────────────────────────────
patch(Orderline.prototype, {
    /**
     * Capture: quantity changes
     * v17: set_quantity is snake_case, parent is this.order
     */
    set_quantity(quantity, keep_price) {
        const oldQty = this.quantity;
        const result = super.set_quantity(...arguments);
        const s = sentinel();

        if (s && oldQty !== this.quantity) {
            try {
                const eventType = this.quantity < 0 ? "negative_qty" : "line_qty_change";
                s.logEvent(eventType, {
                    ...ctx(this),
                    pos_order_id: this.order?.id || false,
                    product_id: this.product?.id || false,
                    amount: (this.price || 0) * (this.quantity || 0),
                    details: {
                        product_name: this.get_full_product_name?.() || "",
                        old_qty: oldQty,
                        new_qty: this.quantity,
                        price_unit: this.price || 0,
                        order_name: this.order?.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] qty_change error:", e);
            }
        }
        return result;
    },

    /**
     * Capture: manual price overrides
     * v17: set_unit_price is snake_case
     */
    set_unit_price(price) {
        const oldPrice = this.price;
        super.set_unit_price(...arguments);
        const s = sentinel();

        if (s && oldPrice !== undefined && oldPrice !== this.price) {
            try {
                const diff = Math.abs(oldPrice - this.price);
                if (diff > 0.01) {
                    s.logEvent("price_override", {
                        ...ctx(this),
                        pos_order_id: this.order?.id || false,
                        product_id: this.product?.id || false,
                        amount: this.price || 0,
                        details: {
                            product_name: this.get_full_product_name?.() || "",
                            old_price: oldPrice,
                            new_price: this.price,
                            qty: this.quantity || 0,
                            order_name: this.order?.name || "",
                        },
                    });
                }
            } catch (e) {
                console.warn("[POS Sentinel] price_override error:", e);
            }
        }
    },

    /**
     * Capture: discount changes
     * v17: set_discount is snake_case
     */
    set_discount(discount) {
        const oldDiscount = this.discount;
        super.set_discount(...arguments);
        const s = sentinel();

        if (s && oldDiscount !== this.discount && this.discount > 0) {
            try {
                s.logEvent("discount", {
                    ...ctx(this),
                    pos_order_id: this.order?.id || false,
                    product_id: this.product?.id || false,
                    amount: (this.price || 0) * (this.quantity || 0) * ((this.discount || 0) / 100),
                    details: {
                        product_name: this.get_full_product_name?.() || "",
                        old_discount: oldDiscount || 0,
                        new_discount: this.discount,
                        price_unit: this.price || 0,
                        qty: this.quantity || 0,
                        order_name: this.order?.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] discount error:", e);
            }
        }
    },
});

// ─── PosStore patches (has env.services) ─────────────────────────
patch(PosStore.prototype, {
    /**
     * Capture: order removal
     * v17: removeOrder (singular, not deleteOrders batch)
     */
    removeOrder(order, removeFromServer) {
        const s = this.env?.services?.pos_sentinel || sentinel();
        if (s && order) {
            try {
                s.logEvent("order_delete", {
                    pos_session_id: this.pos_session?.id ?? false,
                    pos_config_id: this.config?.id ?? false,
                    pos_order_id: order.id || false,
                    amount: order.get_total_with_tax?.() || 0,
                    details: {
                        order_name: order.name || "",
                        line_count: order.get_orderlines?.()?.length || 0,
                        state: order.state || "",
                        partner: order.get_partner?.()?.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] order_delete error:", e);
            }
        }
        return super.removeOrder(...arguments);
    },

    /**
     * Capture: session close + flush pending events
     * v17: closePos (not closeSession)
     */
    async closePos() {
        const s = this.env?.services?.pos_sentinel || sentinel();
        if (s) {
            try {
                s.logEvent("session_close", {
                    pos_session_id: this.pos_session?.id ?? false,
                    pos_config_id: this.config?.id ?? false,
                    details: {
                        session_name: this.pos_session?.name || "",
                        cashier: this.get_cashier?.()?.name || "",
                    },
                });
                await s.flushNow();
            } catch (e) {
                console.warn("[POS Sentinel] session_close error:", e);
            }
        }
        return await super.closePos(...arguments);
    },
});

// ─── PaymentScreen patches ───────────────────────────────────────
patch(PaymentScreen.prototype, {
    /**
     * Capture: order validation (payment completed / refund)
     * v17: validateOrder is camelCase, _isRefundOrder() for refund detection
     */
    async validateOrder(isForceValidate = false) {
        const s = this.env?.services?.pos_sentinel || sentinel();
        const order = this.currentOrder;
        if (s && order) {
            try {
                const isRefund = order._isRefundOrder?.() || false;
                s.logEvent(isRefund ? "refund" : "order_complete", {
                    pos_session_id: order.pos_session_id || false,
                    pos_config_id: order.pos?.config?.id ?? false,
                    pos_order_id: order.id || false,
                    amount: order.get_total_with_tax?.() || 0,
                    details: {
                        order_name: order.name || "",
                        line_count: order.get_orderlines?.()?.length || 0,
                        total: order.get_total_with_tax?.() || 0,
                        is_refund: isRefund,
                        partner: order.get_partner?.()?.name || "",
                        force_validate: isForceValidate,
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] order_complete error:", e);
            }
        }
        return await super.validateOrder(...arguments);
    },
});
