# -*- coding: utf-8 -*-

# Copyright(C) 2012-2019  Budget Insight
#
# This file is part of a weboob module.
#
# This weboob module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This weboob module is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this weboob module. If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import re
import ast
from decimal import Decimal

from datetime import datetime
from weboob.browser.pages import HTMLPage, LoggedPage, JsonPage
from weboob.browser.elements import method, DictElement, ItemElement
from weboob.browser.filters.standard import (
    CleanText, Eval, Env, Map,
)
from weboob.browser.filters.json import Dict
from weboob.capabilities.bank import Account, Investment, Transaction
from weboob.capabilities.base import NotAvailable, empty
from weboob.tools.capabilities.bank.investments import IsinCode, IsinType


def float_to_decimal(f):
    if empty(f):
        return NotAvailable
    return Decimal(str(f))


class LoginPage(HTMLPage):
    def login(self, username, password):
        tab = re.search(r'clavierAChristian = (\[[\d,\s]*\])', self.content).group(1)
        number_list = ast.literal_eval(tab)
        key_map = {}
        for i, number in enumerate(number_list):
            if number < 10:
                key_map[number] = chr(ord('A') + i)
        pass_string = ''.join(key_map[int(n)] for n in password)
        form = self.get_form(name='loginForm')
        form['username'] = username
        form['password'] = pass_string
        form.submit()

    def get_error(self):
        return CleanText('//div[@id="msg"]')(self.doc)


class HomePage(LoggedPage, HTMLPage):
    pass


ACCOUNT_TYPES = {
    'Compte bancaire': Account.TYPE_CHECKING,
    'Epargne bancaire': Account.TYPE_SAVINGS,
    'Crédit': Account.TYPE_LOAN,
    'Epargne': Account.TYPE_LIFE_INSURANCE,
    'Retraite': Account.TYPE_MADELIN,
}


class AccountsPage(LoggedPage, JsonPage):
    @method
    class iter_accounts(DictElement):
        item_xpath = 'entries/*/entries'

        class iter_items(DictElement):
            item_xpath = 'contratItems'

            def parse(self, el):
                self.env['type'] = Dict('libelle')(self)

            class item(ItemElement):
                klass = Account

                def condition(self):
                    # Skip insurances, accounts that are cancelled or replaced,
                    # and accounts that have no available detail
                    return not (
                        Dict('contrat/resilie')(self) or
                        Dict('contrat/remplace')(self) or
                        not Dict('debranchement/hasDetail')(self) or
                        Dict('contrat/produit/classification/categorie')(self) == 'ASSURANCE'
                    )

                obj_id = Dict('contrat/identifiant')
                obj_number = obj_id
                # No IBAN available for now
                obj_iban = NotAvailable
                obj_label = CleanText(Dict('contrat/produit/libelle'))
                obj_type = Map(Env('type'), ACCOUNT_TYPES, Account.TYPE_UNKNOWN)


class AccountDetailsPage(LoggedPage, JsonPage):
    @method
    class fill_account(ItemElement):
        # Some accounts simply don't have any available balance...
        obj_balance = Eval(float_to_decimal, Dict('contrat/montantEpargneContrat', default=None))
        obj_currency = 'EUR'
        # The valuation_diff_ratio is already divided by 100
        obj_valuation_diff_ratio = Eval(float_to_decimal, Dict('contrat/pourcentagePerformanceContrat', default=None))

    def has_investments(self):
        return Dict('contrat/listeSupports', default=None)(self.doc)

    @method
    class iter_investments(DictElement):
        item_xpath = 'contrat/listeSupports'

        class item(ItemElement):
            klass = Investment

            obj_label = CleanText(Dict('libelleSupport'))
            obj_valuation = Eval(float_to_decimal, Dict('montantSupport'))
            obj_quantity = Eval(float_to_decimal, Dict('nbUniteCompte', default=None))
            obj_unitvalue = Eval(float_to_decimal, Dict('valeurUniteCompte', default=None))
            obj_portfolio_share = Eval(lambda x: float_to_decimal(x) / 100, Dict('tauxSupport', default=None))
            obj_code = IsinCode(Dict('codeISIN', default=None), default=NotAvailable)
            obj_code_type = IsinType(Dict('codeISIN', default=None))

            def obj_performance_history(self):
                perfs = {}
                if Dict('detailPerformance/perfSupportUnAn', default=None)(self):
                    perfs[1] = Eval(lambda x: float_to_decimal(x) / 100, Dict('detailPerformance/perfSupportUnAn'))(self)
                if Dict('detailPerformance/perfSupportTroisAns', default=None)(self):
                    perfs[3] = Eval(lambda x: float_to_decimal(x) / 100, Dict('detailPerformance/perfSupportTroisAns'))(self)
                if Dict('detailPerformance/perfSupportCinqAns', default=None)(self):
                    perfs[5] = Eval(lambda x: float_to_decimal(x) / 100, Dict('detailPerformance/perfSupportCinqAns'))(self)
                return perfs


class HistoryPage(LoggedPage, JsonPage):
    @method
    class iter_wealth_history(DictElement):
        item_xpath = '*/historiques'

        class item(ItemElement):
            klass = Transaction

            obj_label = CleanText(Dict('libelle'))
            # There is only one date for each transaction
            obj_date = obj_rdate = Eval(lambda t: datetime.fromtimestamp(int(t) / 1000), Dict('date'))
            obj_type = Transaction.TYPE_BANK

            def obj_amount(self):
                amount = Eval(float_to_decimal, Dict('montant'))(self)
                if Dict('negatif')(self):
                    return -amount
                return amount
