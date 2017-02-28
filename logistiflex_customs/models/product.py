from openerp import netsvc
from openerp.osv import orm, fields
from openerp.addons import decimal_precision as dp
from openerp import SUPERUSER_ID


class PrestashopProductProduct(orm.Model):
    _inherit = 'prestashop.product.product'

    def _prestashop_qty(self, cr, uid, product, context=None):
        return (
            product.suppliers_immediately_usable_qty
            + product.bom_stock
        )


class PrestashopProductCombination(orm.Model):
    _inherit = 'prestashop.product.combination'

    def _prestashop_qty(self, cr, uid, product, context=None):
        return (
            product.suppliers_immediately_usable_qty
            + product.bom_stock
        )


class ProductProduct(orm.Model):
    _inherit = 'product.product'

    def _suppliers_usable_qty(self, cr, uid, ids, field_name, arg,
                              context=None):
        if context is None:
            context = {}
        warehouse_obj = self.pool['stock.warehouse']
        res = {}
        for product in self.browse(cr, uid, ids, context=context):
            quantity = 0.0
            for supplier in product.seller_ids:
                if not supplier.supplier_product_id:
                    continue
                company_id = supplier.supplier_product_id.company_id.id
                warehouse_ids = warehouse_obj.search(
                    cr, SUPERUSER_ID,
                    [('company_id', '=', company_id)],
                    context=context
                )
                for warehouse_id in warehouse_ids:
                    supplier_product = self.browse(
                        cr, SUPERUSER_ID,
                        supplier.supplier_product_id.id,
                        context={'warehouse': warehouse_id}
                    )
                    quantity += supplier_product.bom_stock
            res[product.id] = quantity
        return res

    _columns = {
        'suppliers_immediately_usable_qty': fields.function(
            _suppliers_usable_qty,
            digits_compute=dp.get_precision('Product UoM'),
            type='float',
            string='Suppliers Immediately Usable',
            help="Quantity of products available for sale from our suppliers."
        ),
    }

    def update_prestashop_quantities(self, cr, uid, ids, context=None):
        super(ProductProduct, self).update_prestashop_quantities(
            cr, uid, ids, context=context
        )
        for product in self.browse(cr, uid, ids, context=context):
            for supplierinfo in product.customers_supplierinfo_ids:
                supplierinfo.product_id.update_prestashop_quantities()
        return True

    #fill purchase description with product name on creation(only on creation)
    def create(self, cr, uid, vals, context=None):
        if vals.get('name', False) and not vals.get('description_purchase', False):
            vals['description_purchase'] = vals.get('name')
        return super(ProductProduct, self
                     ).create(cr, uid, vals, context=context)

    # If a 'normal' product change to become a pack : set his stock to 0.
    def write(self, cr, uid, ids, vals, context=None):
        if context is None:
            context = {}
        if vals.get('supply_method', False) == 'produce':
            change_qty_wizard = self.pool['stock.change.product.qty']
            warehouse_obj = self.pool['stock.warehouse']
            warehouse_ids = warehouse_obj.search(cr, uid, [], context=context)
            warehouse = warehouse_obj.browse(cr, uid, warehouse_ids[0],
                context=context)
            for product in self.browse(cr, uid, ids, context=context):
                context['active_id'] = product.id
                if product.qty_available:
                    wizard_vals = {
                        'new_quantity': 0.0,
                        'location_id': warehouse.lot_stock_id.id
                    }
                    wiz_id = change_qty_wizard.create(cr, uid, wizard_vals,
                        context=context)
                    change_qty_wizard.change_product_qty(cr, uid, [wiz_id],
                        context=context)
        return super(ProductProduct, self
                     ).write(cr, uid, ids, vals, context=context)

    def create_automatic_op(self, cr, uid, product_ids, context=None):
        if context is None:
            context = {}
        proc_obj = self.pool.get('procurement.order')
        warehouse_obj = self.pool.get('stock.warehouse')
        wf_service = netsvc.LocalService("workflow")

        warehouse_ids = warehouse_obj.search(cr, uid, [], context=context)
        warehouses = warehouse_obj.browse(
            cr, uid, warehouse_ids, context=context
        )
        proc_ids = []
        for warehouse in warehouses:
            context['warehouse'] = warehouse
            products = self.read(
                cr, uid, product_ids, ['virtual_available'], context=context
            )
            for product_read in products:
                if product_read['virtual_available'] >= 0.0:
                    continue

                product = self.browse(
                    cr, uid, product_read['id'], context=context
                )
                if product.supply_method == 'buy':
                    location_id = warehouse.lot_input_id.id
                elif product.supply_method == 'produce':
                    location_id = warehouse.lot_stock_id.id
                else:
                    continue
                proc_vals = proc_obj._prepare_automatic_op_procurement(
                    cr, uid, product, warehouse, location_id, context=context
                )
                proc_vals['purchase_auto_merge'] = context.get(
                    'purchase_auto_merge', True
                )
                proc_id = proc_obj.create(cr, uid, proc_vals, context=context)
                proc_ids.append(proc_id)
                wf_service.trg_validate(
                    uid, 'procurement.order', proc_id, 'button_confirm', cr
                )
                wf_service.trg_validate(
                    uid, 'procurement.order', proc_id, 'button_check', cr
                )
        return proc_ids

    def get_orderpoint_ids(self, cr, uid, product_ids, context=None):
        orderpoint_obj = self.pool.get('stock.warehouse.orderpoint')
        return orderpoint_obj.search(
            cr, uid,
            [
                ('product_id', 'in', product_ids),
                ('active', '=', True)
            ],
            context=context
        )

    def check_orderpoints_or_automatic(self, cr, uid, product_ids,
                                       context=None):
        proc_ids = []
        for product_id in product_ids:
            op_ids = self.get_orderpoint_ids(
                cr, uid, [product_id], context=context
            )
            if not op_ids:
                proc_ids += self.create_automatic_op(
                    cr, uid, [product_id], context=context
                )
            else:
                proc_ids += self.check_orderpoints(
                    cr, uid, [product_id], context=context
                )
        return proc_ids

    def check_orderpoints(self, cr, uid, product_ids, context=None):
        if context is None:
            context = {}
        orderpoint_obj = self.pool.get('stock.warehouse.orderpoint')
        op_ids = self.get_orderpoint_ids(cr, uid, product_ids, context=context)
        proc_obj = self.pool.get('procurement.order')
        wf_service = netsvc.LocalService("workflow")
        proc_ids = []
        for op in orderpoint_obj.browse(cr, uid, op_ids, context=context):
            prods = proc_obj._product_virtual_get(cr, uid, op)
            if prods is None or prods >= op.product_min_qty:
                continue

            qty = max(op.product_min_qty, op.product_max_qty)-prods

            reste = qty % op.qty_multiple
            if reste != 0:
                if op.product_max_qty:
                    qty -= reste
                else:
                    qty += op.qty_multiple - reste

            if qty <= 0:
                continue
            if op.product_id.type != 'consu' and op.procurement_draft_ids:
                # Check draft procurement related to this order point
                pro_ids = [x.id for x in op.procurement_draft_ids]
                procure_datas = proc_obj.read(
                    cr, uid, pro_ids, ['id', 'product_qty'], context=context
                )
                to_generate = qty
                for proc_data in procure_datas:
                    if to_generate >= proc_data['product_qty']:
                        wf_service.trg_validate(
                            uid,
                            'procurement.order',
                            proc_data['id'],
                            'button_confirm',
                            cr
                        )
                        proc_obj.write(
                            cr, uid,
                            [proc_data['id']],
                            {'origin': op.name},
                            context=context
                        )
                        to_generate -= proc_data['product_qty']
                    if not to_generate:
                        break
                qty = to_generate

            if qty:
                proc_vals = proc_obj._prepare_orderpoint_procurement(
                    cr, uid, op, qty, context=context
                )
                proc_vals['purchase_auto_merge'] = context.get(
                    'purchase_auto_merge', True
                )
                proc_id = proc_obj.create(cr, uid, proc_vals, context=context)
                proc_ids.append(proc_id)
                wf_service.trg_validate(
                    uid, 'procurement.order', proc_id, 'button_confirm', cr
                )
                orderpoint_obj.write(
                    cr, uid, [op.id],
                    {'procurement_id': proc_id},
                    context=context
                )
                wf_service.trg_validate(
                    uid, 'procurement.order', proc_id, 'button_check', cr
                )
        return proc_ids


class ProductSupplierinfo(orm.Model):
    _inherit = 'product.supplierinfo'

    def write(self, cr, uid, ids, vals, context=None):
        product_obj = self.pool['product.product']
        res = super(ProductSupplierinfo, self
                     ).write(cr, uid, ids, vals, context=context)
        if vals.get('supplier_product_id', False):
            for sup in self.browse(cr, uid, ids, context=context):
                product_obj.write(cr, uid, [sup.product_id.id],
                                  {'procure_method': 'make_to_order'},
                                  context=context)
        else:
            for sup in self.browse(cr, uid, ids, context=context):
                if not sup.supplier_product_id:
                    product_obj.write(cr, uid, [sup.product_id.id],
                                      {'procure_method': 'make_to_stock'},
                                      context=context)
        return res

    def create(self, cr, uid, vals, context=None):
        product_obj = self.pool['product.product']
        if vals.get('supplier_product_id', False):
            product_obj.write(cr, uid, [vals['product_id']],
                              {'procure_method': 'make_to_order'},
                              context=context)
        return super(ProductSupplierinfo, self
                     ).create(cr, uid, vals, context=context)

