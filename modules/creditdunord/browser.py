# -*- coding: utf-8 -*-

# Copyright(C) 2012 Romain Bignon
#
# This file is part of weboob.
#
# weboob is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# weboob is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with weboob. If not, see <http://www.gnu.org/licenses/>.


import re
import urllib

from weboob.tools.browser import BaseBrowser, BrowserIncorrectPassword

from .pages import LoginPage, AccountsPage, TransactionsPage


__all__ = ['CreditDuNordBrowser']


class CreditDuNordBrowser(BaseBrowser):
    PROTOCOL = 'https'
    ENCODING = 'UTF-8'
    PAGES = {'https://[^/]+/?':                                         LoginPage,
             'https://[^/]+/.*\?.*_pageLabel=page_erreur_connexion':    LoginPage,
             'https://[^/]+/vos-comptes/particuliers(\?.*)?':           AccountsPage,
             'https://[^/]+/vos-comptes/.*/transac/.*':                 TransactionsPage,
            }

    def __init__(self, website, *args, **kwargs):
        self.DOMAIN = website
        BaseBrowser.__init__(self, *args, **kwargs)

    def is_logged(self):
        return self.page is not None and not self.is_on_page(LoginPage)

    def home(self):
        if self.is_logged():
            self.location(self.buildurl('/vos-comptes/particuliers'))
        else:
            self.login()
        return
        return self.location(self.buildurl('/vos-comptes/particuliers'))

    def login(self):
        assert isinstance(self.username, basestring)
        assert isinstance(self.password, basestring)

        # not necessary (and very slow)
        #self.location('https://www.credit-du-nord.fr/', no_login=True)

        m = re.match('www.([^\.]+).fr', self.DOMAIN)
        if not m:
            bank_name = 'credit-du-nord'
            self.logger.error('Unable to find bank name for %s' % self.DOMAIN)
        else:
            bank_name = m.group(1)

        data = {'bank':         bank_name,
                'pagecible':    'vos-comptes',
                'password':     self.password.encode(self.ENCODING),
                'pwAuth':       'Authentification+mot+de+passe',
                'username':     self.username.encode(self.ENCODING),
               }

        self.location(self.buildurl('/saga/authentification'), urllib.urlencode(data), no_login=True)

        if not self.is_logged():
            raise BrowserIncorrectPassword()

    def get_accounts_list(self):
        if not self.is_on_page(AccountsPage):
            self.location(self.buildurl('/vos-comptes/particuliers'))
        return self.page.get_list()

    def get_account(self, id):
        assert isinstance(id, basestring)

        l = self.get_accounts_list()
        for a in l:
            if a.id == id:
                return a

        return None

    def iter_transactions(self, link, link_id, execution, is_coming=None):
        if link_id is None:
            return

        event = 'clicDetailCompte'
        while 1:
            data = {'_eventId':         event,
                    '_ipc_eventValue':  '',
                    '_ipc_fireEvent':   '',
                    'deviseAffichee':   'DEVISE',
                    'execution':        execution,
                    'idCompteClique':   link_id,
                   }
            self.location(link, urllib.urlencode(data))

            assert self.is_on_page(TransactionsPage)

            self.page.is_coming = is_coming

            for tr in self.page.get_history():
                yield tr

            is_last = self.page.is_last()
            if is_last:
                return

            event = 'clicChangerPageSuivant'
            execution = self.page.get_execution()
            is_coming = self.page.is_coming

    def get_history(self, account):
        for tr in self.iter_transactions(account._link, account._link_id, account._execution):
            yield tr

        for tr in self.get_card_operations(account):
            yield tr

    def get_card_operations(self, account):
        for link_id in account._card_ids:
            for tr in self.iter_transactions(account._link, link_id, account._execution, True):
                yield tr
