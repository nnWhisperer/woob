# Copyright(C) 2019 Powens
#
# This file is part of a woob module.
#
# This woob module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This woob module is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this woob module. If not, see <http://www.gnu.org/licenses/>.

# flake8: compatible

import re
from datetime import date
from time import time

from dateutil.relativedelta import relativedelta

from woob.browser import URL, need_login
from woob.exceptions import (
    ActionNeeded, ActionType, BrowserIncorrectPassword, BrowserPasswordExpired,
    BrowserUnavailable,
)
from woob.tools.capabilities.bill.documents import merge_iterators
from woob_modules.franceconnect.browser import FranceConnectBrowser

from .pages import (
    CguPage, CtPage, DocumentsDetailsPage, DocumentsFirstSummaryPage, DocumentsLastSummaryPage,
    ErrorPage, FranceConnectRedirectPage, LoginPage, NewPasswordPage, RedirectPage, SubscriptionPage,
)


class AmeliBrowser(FranceConnectBrowser):
    BASEURL = 'https://assure.ameli.fr'

    france_connect_redirect = URL(r'/PortailAS/FranceConnect', FranceConnectRedirectPage)
    error_page = URL(r'/vu/INDISPO_COMPTE_ASSURES.html', ErrorPage)
    login_page = URL(
        r'/PortailAS/appmanager/PortailAS/assure\?_nfpb=true&connexioncompte_2actionEvt=afficher.*',
        r'/PortailAS/appmanager/PortailAS/assure\?_nfpb=true&.*validationconnexioncompte.*',
        LoginPage
    )
    new_password_page = URL(
        r'/PortailAS/appmanager/PortailAS/assure\?.*as_modif_code_perso_ameli_apres_reinit_page',
        NewPasswordPage
    )
    redirect_page = URL(
        r'/PortailAS/appmanager/PortailAS/assure\?_nfpb=true&.*validationconnexioncompte.*',
        RedirectPage
    )
    cgu_page = URL(
        r'/PortailAS/appmanager/PortailAS/assure\?_nfpb=true&_pageLabel=as_conditions_generales_page.*',
        CguPage
    )
    subscription_page = URL(
        r'/PortailAS/appmanager/PortailAS/assure\?_nfpb=true&_pageLabel=as_info_perso_page.*',
        SubscriptionPage
    )
    documents_details_page = URL(r'/PortailAS/paiements.do', DocumentsDetailsPage)
    documents_first_summary_page = URL(
        r'PortailAS/appmanager/PortailAS/assure\?_nfpb=true&_pageLabel=as_releve_mensuel_paiement_page',
        DocumentsFirstSummaryPage
    )
    documents_last_summary_page = URL(
        r'PortailAS/portlets/relevemensuelpaiement/relevemensuelpaiement.do\?actionEvt=afficherPlusReleves',
        DocumentsLastSummaryPage
    )
    ct_page = URL(r'/PortailAS/JavaScriptServlet', CtPage)

    def __init__(self, config, *args, **kwargs):
        kwargs['username'] = config['login'].get()
        kwargs['password'] = config['password'].get()
        self.login_source = config['login_source'].get()
        super(AmeliBrowser, self).__init__(*args, **kwargs)

    def do_login(self):
        if self.login_source == 'direct':
            self.direct_login()
        else:
            self.france_connect_redirect.go()
            if self.login_source == 'fc_ameli':
                self.login_ameli()
            elif self.login_source == 'fc_impots':
                self.login_impots()
            else:
                raise AssertionError(f'Unexpected login source: {self.login_source}')

        if self.cgu_page.is_here():
            raise ActionNeeded(self.page.get_cgu_message(), action_type=ActionType.ACKNOWLEDGE)

    def direct_login(self):
        self.login_page.go()
        if self.page.is_direct_login_disabled():
            raise BrowserUnavailable()

        # _ct value is necessary for the login
        _ct = self.ct_page.open(method='POST', headers={'FETCH-CSRF-TOKEN': '1'}).get_ct_value()
        self.page.login(self.username, self.password, _ct)
        if self.new_password_page.is_here():
            raise BrowserPasswordExpired()
        if self.login_page.is_here():
            err_msg = self.page.get_error_message()
            wrongpass_regex = re.compile(
                'numéro de sécurité sociale et le code personnel'
                + '|compte ameli verrouillé'
                + '|le mot de passe ne correspondent pas'
            )
            if wrongpass_regex.search(err_msg):
                raise BrowserIncorrectPassword(err_msg)
            raise AssertionError('Unhandled error at login %s' % err_msg)

    @need_login
    def iter_subscription(self):
        self.subscription_page.go()
        yield self.page.get_subscription()

    @need_login
    def _iter_details_documents(self, subscription):
        end_date = date.today()

        start_date = end_date - relativedelta(years=1)

        params = {
            'Beneficiaire': 'tout_selectionner',
            'DateDebut': start_date.strftime('%d/%m/%Y'),
            'DateFin': end_date.strftime('%d/%m/%Y'),
            'actionEvt': 'Rechercher',
            'afficherIJ': 'false',
            'afficherInva': 'false',
            'afficherPT': 'false',
            'afficherRS': 'false',
            'afficherReleves': 'false',
            'afficherRentes': 'false',
            'idNoCache': int(time() * 1000),
        }

        # website tell us details documents are available for 6 months
        self.documents_details_page.go(params=params)
        return self.page.iter_documents(subid=subscription.id)

    @need_login
    def _iter_summary_documents(self, subscription):
        # The monthly statements for the last 23 months are available in two parts.
        # The first part contains the last 6 months on an HTML page.
        self.documents_first_summary_page.go()
        for doc in self.page.iter_documents(subid=subscription.id):
            yield doc

        # The second part is retrieved in JSON via this page which displays the next 6 months at each iteration.
        for _ in range(3):
            self.documents_last_summary_page.go()
            for doc in self.page.iter_documents(subid=subscription.id):
                yield doc

    @need_login
    def iter_documents(self, subscription):
        for doc in merge_iterators(
            self._iter_details_documents(subscription),
            self._iter_summary_documents(subscription)
        ):
            yield doc

    @need_login
    def get_profile(self):
        self.subscription_page.go()
        return self.page.get_profile()
