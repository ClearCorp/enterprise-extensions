# -*- coding: utf-8 -*-
##############################################################################
# For copyright and license notices, see __openerp__.py file in module root
# directory
##############################################################################

from dateutil.relativedelta import relativedelta
import datetime
import logging
import time
from openerp import models, fields, api, _
from openerp.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class PurchaseSubscription(models.Model):
    _name = "purchase.subscription"
    _description = "Purchase Subscription"
    _inherit = "sale.subscription"

    @api.multi
    @api.depends('recurring_invoice_line_ids')
    def _get_recurring_price(self):
        for account in self:
            account.recurring_total = sum(
                line.price_subtotal for line in account.
                recurring_invoice_line_ids)

    recurring_invoice_line_ids = fields.One2many(
        'purchase.subscription.line',
        'analytic_account_id', 'Invoice Lines', copy=True)
    recurring_total = fields.Float(
        compute='_get_recurring_price',
        string="Recurring Price",
        store=True, track_visibility='onchange')
    template_id = fields.Many2one(
        'purchase.subscription',
        'Subscription Template',
        domain=[('type', '=', 'template')],
        track_visibility='onchange')

    @api.multi
    def _prepare_invoice_data(self):
        journal_obj = self.env['account.journal']
        invoice = {}
        if not self.partner_id:
            raise ValidationError(_(
                "No Supplier Defined!\n"
                "You must first select a Supplier for "
                "Contract %s!") % self.name)

        fpos = self.partner_id.property_account_position_id or False
        journal_ids = journal_obj.search([(
            'type', '=', 'purchase'), ('company_id', '=', self.
                                       company_id.id)], limit=1)
        if not journal_ids:
            raise ValidationError(_(
                'Please define a pruchase journal for the company "%s".') % (
                self.company_id.name or '', ))
        invoice = {
            'account_id': self.partner_id.property_account_payable_id.id,
            'type': 'in_invoice',
            'reference': self.name,
            'partner_id': self.partner_id.id,
            'currency_id': self.company_id.currency_id.id,
            'journal_id': len(journal_ids) and journal_ids[0].id or False,
            'date_invoice': self.recurring_next_date,
            'origin': self.code,
            'fiscal_position_id': fpos and fpos.id,
            'company_id': self.company_id.id,
        }
        return invoice

    @api.model
    def _prepare_invoice_lines(self, fiscal_position_id):

        fiscal_position = self.env['account.fiscal.position'].browse(
            fiscal_position_id)

        invoice_lines = []
        for line in self.recurring_invoice_line_ids:

            res = line.product_id
            account_id = res.property_account_expense_id.id
            if not account_id:
                account_id = res.categ_id.property_account_expense_categ_id.id
            account_id = fiscal_position.map_account(account_id)

            taxes = res.supplier_taxes_id.filtered(
                lambda r: r.company_id == line.
                analytic_account_id.company_id)

            taxes = fiscal_position.map_tax(taxes)

            invoice_lines.append((0, 0, {
                'name': line.name,
                'account_id': account_id,
                'account_analytic_id': (
                    line.analytic_account_id.analytic_account_id.id),
                'price_unit': line.price_unit or 0.0,
                'quantity': line.quantity,
                'uom_id': line.uom_id.id or False,
                'product_id': line.product_id.id or False,
                'invoice_line_tax_ids': [(6, 0, taxes.ids)],
            }))
        return invoice_lines

    @api.model
    def _cron_recurring_create_invoice_purchase(self):
        current_date = time.strftime('%Y-%m-%d')
        contract_ids = self.search(
            [('recurring_next_date', '<=', current_date), (
                'state', '=', 'open')])
        return contract_ids._recurring_create_invoice()

    @api.multi
    def _prepare_invoice(self):
        invoice = self._prepare_invoice_data()
        invoice['invoice_line_ids'] = self._prepare_invoice_lines(
            invoice['fiscal_position_id'])
        return invoice

    @api.multi
    def _recurring_create_invoice(self, automatic=False):
        invoice_ids = []
        current_date = time.strftime('%Y-%m-%d')
        ids = [c.id for c in self]
        if ids:
            contract_ids = ids
        else:
            contract_ids = self.search([(
                'recurring_next_date', '<=', current_date),
                ('state', '=', 'open'), ('type', '=', 'contract')])

        if contract_ids:
            self.env.cr.execute(
                'SELECT a.company_id, array_agg(psub.id) as ids '
                'FROM purchase_subscription as psub JOIN'
                ' account_analytic_account as a'
                ' ON psub.analytic_account_id = a.id '
                'WHERE psub.id IN %s GROUP BY a.company_id',
                (tuple(contract_ids),))
            for company_id, ids in self._cr.fetchall():
                context_company = dict(
                    company_id=company_id, force_company=company_id)
                for contract in self.with_context(context_company):
                    try:
                        invoice_values = contract._prepare_invoice()
                        invoice_ids.append(self.env['account.invoice'].create(
                            invoice_values))
                        invoice_ids[-1].compute_taxes()
                        next_date = datetime.datetime.strptime(
                            contract.recurring_next_date or current_date,
                            "%Y-%m-%d")
                        interval = contract.recurring_interval
                        if contract.recurring_rule_type == 'daily':
                            new_date = next_date + \
                                relativedelta(days=+interval)
                        elif contract.recurring_rule_type == 'weekly':
                            new_date = next_date + \
                                relativedelta(weeks=+interval)
                        elif contract.recurring_rule_type == 'monthly':
                            new_date = next_date + \
                                relativedelta(months=+interval)
                        else:
                            new_date = next_date + \
                                relativedelta(years=+interval)
                        contract.write({
                            'recurring_next_date': new_date.
                            strftime('%Y-%m-%d')})
                        if automatic:
                            self._cr.commit()
                    except Exception:
                        if automatic:
                            self._cr.rollback()
                            _logger.exception(
                                'Fail to create recurring invoice '
                                'for contract %s', contract.code)
                        else:
                            raise
        return invoice_ids

    @api.multi
    def action_subscription_invoice(self):
        analytic_ids = [sub.analytic_account_id.id for sub in self]
        invoice_ids = self.env['account.invoice'].search(
            [('invoice_line_ids.account_analytic_id', 'in', analytic_ids),
             ('origin', 'in', [sub.code for sub in self])])
        imd = self.env['ir.model.data']
        list_view_id = imd.xmlid_to_res_id('account.invoice_supplier_tree')
        form_view_id = imd.xmlid_to_res_id('account.invoice_supplier_form')
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.invoice",
            "views": [[list_view_id, "tree"], [form_view_id, "form"]],
            "domain": [["id", "in", invoice_ids.ids]],
            "context": {"create": False},
        }

    @api.model
    def cron_account_analytic_account(self):
        remind = {}

        def fill_remind(key, domain, write_pending=False):
            base_domain = [
                ('type', '=', 'contract'),
                ('partner_id', '!=', False),
                ('manager_id', '!=', False),
                ('manager_id.email', '!=', False),
            ]
            base_domain.extend(domain)
            accounts = self.search(base_domain, order='name asc')
            for account in accounts:
                if write_pending:
                    account.write({'state': 'pending'})
                remind_user = remind.setdefault(account.manager_id.id, {})
                remind_type = remind_user.setdefault(key, {})
                remind_partner = remind_type.setdefault(
                    account.partner_id, []).append(account)

        # Already expired
        fill_remind("old", [('state', 'in', ['pending'])])

        # Expires now
        fill_remind("new", [('state', 'in', ['draft', 'open']),
                            '&', ('date', '!=', False),
                            ('date', '<=', time.strftime('%Y-%m-%d')),
                            ], True)

        # Expires in less than 30 days
        fill_remind("future", [
            ('state', 'in', ['draft', 'open']),
            ('date', '!=', False),
            ('date', '<', (datetime.datetime.now() + datetime
                           .timedelta(30)).strftime("%Y-%m-%d"))])
        base_url = self.env['ir.config_parameter'].get_param(
            'web.base.url')
        action_id = self.env['ir.model.data'].get_object_reference(
            'purchase_contract', 'purchase_subscription_action')[1]
        template_id = self.env['ir.model.data'].get_object_reference(
            'purchase_contract',
            'purchase_account_analytic_cron_email_template')[1]
        for user_id, data in remind.items():
            _logger.debug("Sending reminder to uid %s", user_id)
            self.env['mail.template'].browse(template_id).with_context(
                base_url=base_url, action_id=action_id, data=data).send_mail(
                user_id, force_send=True)
        return True


class PurchaseSubscriptionLine(models.Model):
    _name = "purchase.subscription.line"
    _inherit = "sale.subscription.line"

    analytic_account_id = fields.Many2one(
        'purchase.subscription', 'Subscription', ondelete='cascade')
    quantity = fields.Float('Quantity', compute='_compute_quantity')
    purchase_quantity = fields.Float('Purchase Quantity')

    @api.multi
    @api.depends('purchase_quantity')
    def _compute_quantity(self):
        for rec in self:
            rec.quantity = rec.purchase_quantity
