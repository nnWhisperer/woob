# -*- coding: utf-8 -*-

# Copyright(C) 2012-2020  Budget Insight
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


from weboob.browser import LoginBrowser, URL, need_login
from weboob.exceptions import BrowserIncorrectPassword, BrowserUnavailable
from weboob.capabilities.bank import Account, Investment
from weboob.capabilities.base import find_object

from .pages import LoginPage, ProfilePage, AccountTypePage, AccountsPage, ProAccountsPage, TransactionsPage, \
                   IbanPage, RedirectPage, EntryPage, AVPage, ProIbanPage, ProTransactionsPage


class KolbBrowser(LoginBrowser):
    ENCODING = 'UTF-8'

    BASEURL = "https://www.banque-kolb.fr"
    login = URL('$',
                '/.*\?.*_pageLabel=page_erreur_connexion',                        LoginPage)
    redirect = URL('/swm/redirectCDN.html',                                       RedirectPage)
    entrypage = URL('/icd/zco/#zco', EntryPage)
    multitype_av = URL('/vos-comptes/IPT/appmanager/transac/professionnels\?_nfpb=true&_eventName=onRestart&_pageLabel=synthese_contrats_assurance_vie', AVPage)
    proaccounts = URL('/vos-comptes/IPT/appmanager/transac/(professionnels|entreprises)\?_nfpb=true&_eventName=onRestart&_pageLabel=transac_tableau_de_bord', ProAccountsPage)
    accounts = URL('/vos-comptes/IPT/appmanager/transac/(?P<account_type>.*)\?_nfpb=true&_eventName=onRestart&_pageLabel=transac_tableau_de_bord', AccountsPage)
    synthesis = URL('/vos-comptes/IPT/appmanager/transac/(?P<account_type>.*)\?_nfpb=true&_eventName=onRestart&_pageLabel=page_synthese_v1', AccountsPage)
    proloans = URL('/vos-comptes/IPT/appmanager/transac/(?P<account_type>.*)\?_nfpb=true&_eventName=onRestart&_pageLabel=credit_en_cours', ProAccountsPage)
    loans = URL('/vos-comptes/IPT/appmanager/transac/particuliers\?_nfpb=true&_eventName=onRestart&_pageLabel=creditPersoImmobilier', ProAccountsPage)
    multitype_iban = URL('/vos-comptes/IPT/appmanager/transac/professionnels\?_nfpb=true&_eventName=onRestart&_pageLabel=impression_rib', ProIbanPage)
    transactions = URL('/vos-comptes/IPT/appmanager/transac/particuliers\?_nfpb=true(.*)', TransactionsPage)
    protransactions = URL('/vos-comptes/(.*)/transac/(professionnels|entreprises)', ProTransactionsPage)
    iban = URL('/vos-comptes/IPT/cdnProxyResource/transacClippe/RIB_impress.asp', IbanPage)
    account_type_page = URL("/icd/zco/public-data/public-ws-menuespaceperso.json", AccountTypePage)
    profile_page = URL("/icd/zco/data/user.json", ProfilePage)


    def __init__(self, *args, **kwargs):
        self.weboob = kwargs['weboob']
        super(KolbBrowser, self).__init__(*args, **kwargs)

    def is_logged(self):
        return self.page is not None and not self.login.is_here() and \
            not self.page.doc.xpath(u'//b[contains(text(), "vous devez modifier votre code confidentiel")]')

    def home(self):
        if self.is_logged():
            self.location("/icd/zco/")
            self.accounts.go(account_type=self.account_type)
        else:
            self.do_login()

    def go_synthesis(self):
        self.synthesis.go(account_type=self.account_type)

    def do_login(self):
        self.login.go().login(self.username, self.password)
        if self.accounts.is_here():
            expired_error = self.page.get_password_expired()
            if expired_error:
                raise BrowserUnavailable(expired_error)

        if self.login.is_here():
            error = self.page.get_error()
            if error:
                raise BrowserIncorrectPassword(error)
            else:
                # in case we are still on login without error message
                # we'll check what's happening.
                assert False, "Still on login page."

        if not self.is_logged():
            raise BrowserIncorrectPassword()

    def _iter_accounts(self):
        if self.account_type == "particuliers":
            self.loans.go()
        else:
            self.proloans.go(account_type=self.account_type)
        for a in self.page.get_list():
            yield a
        self.go_synthesis()
        self.multitype_av.go()
        if self.multitype_av.is_here():
            for a in self.page.get_av_accounts():
                self.location(a._link, data=a._args)
                self.location(a._link.replace("_attente", "_detail_contrat_rep"), data=a._args)
                self.page.fill_diff_currency(a)
                yield a
        self.go_synthesis()
        self.accounts.go(account_type=self.account_type)
        for a in self.page.get_list():
            yield a

    @need_login
    def get_accounts_list(self):
        self.account_type_page.go()
        self.account_type = self.page.get_account_type()

        accounts = list(self._iter_accounts())
        self.multitype_iban.go()
        link = self.page.iban_go()

        for a in [a for a in accounts if a._acc_nb]:
            self.location(link + a._acc_nb)
            a.iban = self.page.get_iban()

        return accounts

    def get_account(self, id):
        account_list = self.get_accounts_list()
        return find_object(account_list, id=id)

    @need_login
    def get_account_for_history(self, id):
        account_list = list(self._iter_accounts())
        return find_object(account_list, id=id)

    @need_login
    def iter_transactions(self, link, args, acc_type):
        if args is None:
            return
        while args is not None:
            self.location(link, data=args)
            assert (self.transactions.is_here() or self.protransactions.is_here())
            for tr in self.page.get_history(acc_type):
                yield tr

            args = self.page.get_next_args(args)

    @need_login
    def get_history(self, account, coming=False):
        if coming and account.type != Account.TYPE_CARD or account.type == Account.TYPE_LOAN:
            return
        for tr in self.iter_transactions(account._link, account._args, account.type):
            yield tr

    @need_login
    def get_investment(self, account):
        investments = []

        if 'LIQUIDIT' in account.label:
            inv = Investment()
            inv.code = 'XX-Liquidity'
            inv.label = 'Liquidité'
            inv.valuation = account.balance
            investments.append(inv)
            return investments

        if not account._inv:
            return []

        if account.type in (Account.TYPE_MARKET, Account.TYPE_PEA):
            self.location(account._link, data=account._args)
            if self.page.can_iter_investments():
                return self.page.get_market_investment()
        elif (account.type == Account.TYPE_LIFE_INSURANCE):
            self.location(account._link, data=account._args)
            self.location(account._link.replace("_attente", "_detail_contrat_rep"), data=account._args)
            if self.page.can_iter_investments():
                return self.page.get_deposit_investment()
        return investments

    @need_login
    def get_profile(self):
        self.profile_page.go()
        return self.page.get_profile()
