/** @odoo-module */

/**
 * POS Sentinel — Shadow Logger
 *
 * Patches POS models (PosOrder, PosOrderline) and components (PosStore,
 * PaymentScreen) to silently capture fraud-relevant events.
 *
 * POS models (PosOrder, PosOrderline) don't have access to env.services,
 * so we use window.__posSentinel (set by sentinel_service.js).
 * POS components (PosStore, PaymentScreen) use this.env.services.
 *
 * Design principles:
 * - Fire-and-forget: errors never block the POS UI
 * - Zero overhead: no popups, no delays for the cashier
 * - Captures old/new values for forensic analysis
 */

import { patch } from "@web/core/utils/patch";
import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { PosOrderline } from "@point_of_sale/app/models/pos_order_line";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

// ─── Helpers ─────────────────────────────────────────────────────

/** Get sentinel from global (for model patches without env) */
function sentinel() {
    return window.__posSentinel || null;
}

/** Extract POS context from a model instance */
function ctx(obj) {
    try {
        return {
            pos_session_id: obj.session?.id ?? false,
            pos_config_id: obj.config?.id ?? false,
        };
    } catch {
        return { pos_session_id: false, pos_config_id: false };
    }
}

// ─── PosOrder patches ────────────────────────────────────────────
patch(PosOrder.prototype, {
    /**
     * Capture: line removal (void)
     */
    removeOrderline(line) {
        const s = sentinel();
        if (s && line) {
            try {
                s.logEvent("void_line", {
                    ...ctx(this),
                    pos_order_id: this.id || false,
                    product_id: line.product_id?.id || false,
                    amount: (line.price_unit || 0) * (line.qty || 0),
                    details: {
                        product_name: line.getFullProductName?.() || "",
                        qty: line.qty || 0,
                        price_unit: line.price_unit || 0,
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
     */
    removePaymentline(line) {
        const s = sentinel();
        if (s && line) {
            try {
                s.logEvent("payment_change", {
                    ...ctx(this),
                    pos_order_id: this.id || false,
                    amount: line.amount || 0,
                    details: {
                        action: "remove_payment",
                        payment_method: line.payment_method_id?.name || "",
                        amount: line.amount || 0,
                        order_name: this.name || "",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] payment_change error:", e);
            }
        }
        return super.removePaymentline(...arguments);
    },

    /**
     * Capture: payment line added
     */
    addPaymentline(payment_method) {
        const result = super.addPaymentline(...arguments);
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

// ─── PosOrderline patches ────────────────────────────────────────
patch(PosOrderline.prototype, {
    /**
     * Capture: quantity changes
     */
    setQuantity(quantity, keep_price) {
        const oldQty = this.qty;
        const result = super.setQuantity(...arguments);
        const s = sentinel();

        if (s && oldQty !== this.qty) {
            try {
                const eventType = this.qty < 0 ? "negative_qty" : "line_qty_change";
                s.logEvent(eventType, {
                    ...ctx(this),
                    pos_order_id: this.order_id?.id || false,
                    product_id: this.product_id?.id || false,
                    amount: (this.price_unit || 0) * (this.qty || 0),
                    details: {
                        product_name: this.getFullProductName?.() || "",
                        old_qty: oldQty,
                        new_qty: this.qty,
                        price_unit: this.price_unit || 0,
                        order_name: this.order_id?.name || "",
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
     */
    setUnitPrice(price) {
        const oldPrice = this.price_unit;
        super.setUnitPrice(...arguments);
        const s = sentinel();

        // Only log meaningful price changes (not initial setup)
        if (s && oldPrice !== undefined && oldPrice !== this.price_unit) {
            try {
                const diff = Math.abs(oldPrice - this.price_unit);
                if (diff > 0.01) {
                    s.logEvent("price_override", {
                        ...ctx(this),
                        pos_order_id: this.order_id?.id || false,
                        product_id: this.product_id?.id || false,
                        amount: this.price_unit || 0,
                        details: {
                            product_name: this.getFullProductName?.() || "",
                            old_price: oldPrice,
                            new_price: this.price_unit,
                            qty: this.qty || 0,
                            price_type: this.price_type || "original",
                            order_name: this.order_id?.name || "",
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
     */
    setDiscount(discount) {
        const oldDiscount = this.discount;
        super.setDiscount(...arguments);
        const s = sentinel();

        if (s && oldDiscount !== this.discount && this.discount > 0) {
            try {
                s.logEvent("discount", {
                    ...ctx(this),
                    pos_order_id: this.order_id?.id || false,
                    product_id: this.product_id?.id || false,
                    amount: (this.price_unit || 0) * (this.qty || 0) * ((this.discount || 0) / 100),
                    details: {
                        product_name: this.getFullProductName?.() || "",
                        old_discount: oldDiscount || 0,
                        new_discount: this.discount,
                        price_unit: this.price_unit || 0,
                        qty: this.qty || 0,
                        order_name: this.order_id?.name || "",
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
     * Capture: order deletion (batch)
     */
    async deleteOrders(orders, serverIds = [], ignoreChange = false) {
        const s = this.env?.services?.pos_sentinel || sentinel();
        if (s && orders?.length) {
            for (const order of orders) {
                try {
                    s.logEvent("order_delete", {
                        pos_session_id: this.session?.id ?? false,
                        pos_config_id: this.config?.id ?? false,
                        pos_order_id: order.id || false,
                        amount: order.priceIncl || 0,
                        details: {
                            order_name: order.name || "",
                            line_count: order.lines?.length || 0,
                            state: order.state || "",
                            partner: order.partner_id?.name || "",
                        },
                    });
                } catch (e) {
                    console.warn("[POS Sentinel] order_delete error:", e);
                }
            }
        }
        return await super.deleteOrders(...arguments);
    },

    /**
     * Capture: cash in / cash out
     * Note: cashMove() opens a popup that doesn't return payload data,
     * so we log the event occurrence without specific amount.
     */
    async cashMove() {
        const s = this.env?.services?.pos_sentinel || sentinel();
        if (s) {
            try {
                s.logEvent("cash_out", {
                    pos_session_id: this.session?.id ?? false,
                    pos_config_id: this.config?.id ?? false,
                    amount: 0,
                    details: {
                        action: "cash_move_initiated",
                    },
                });
            } catch (e) {
                console.warn("[POS Sentinel] cash_move error:", e);
            }
        }
        return await super.cashMove(...arguments);
    },

    /**
     * Capture: session close + flush pending events
     */
    async closeSession() {
        const s = this.env?.services?.pos_sentinel || sentinel();
        if (s) {
            try {
                s.logEvent("session_close", {
                    pos_session_id: this.session?.id ?? false,
                    pos_config_id: this.config?.id ?? false,
                    details: {
                        session_name: this.session?.name || "",
                        cashier: this.getCashier?.()?.name || "",
                    },
                });
                await s.flushNow();
            } catch (e) {
                console.warn("[POS Sentinel] session_close error:", e);
            }
        }
        return await super.closeSession(...arguments);
    },
});

// ─── PaymentScreen patches ───────────────────────────────────────
patch(PaymentScreen.prototype, {
    /**
     * Capture: order validation (payment completed / refund)
     */
    async validateOrder(isForceValidate = false) {
        const s = this.env?.services?.pos_sentinel || sentinel();
        const order = this.currentOrder;
        if (s && order) {
            try {
                const isRefund = order.isRefund;
                s.logEvent(isRefund ? "refund" : "order_complete", {
                    pos_session_id: order.session?.id ?? false,
                    pos_config_id: order.config?.id ?? false,
                    pos_order_id: order.id || false,
                    amount: order.priceIncl || 0,
                    details: {
                        order_name: order.name || "",
                        line_count: order.lines?.length || 0,
                        total: order.priceIncl || 0,
                        is_refund: isRefund || false,
                        partner: order.partner_id?.name || "",
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
