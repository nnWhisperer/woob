# Copyright(C) 2012 Romain Bignon
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

import json
import re
from collections import OrderedDict
from datetime import timedelta
from functools import wraps
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from requests.exceptions import ReadTimeout

from woob.browser import URL, need_login
from woob.browser.adapters import LowSecHTTPAdapter
from woob.browser.exceptions import ClientError, HTTPNotFound, ServerError
from woob.browser.mfa import TwoFactorBrowser
from woob.browser.pages import FormNotFound
from woob.capabilities.bank import Account, AccountOwnership, Loan
from woob.capabilities.base import NotAvailable, find_object
from woob.exceptions import (
    AppValidation, AppValidationExpired, AuthMethodNotImplemented, BrowserIncorrectPassword,
    BrowserUnavailable, OfflineOTPQuestion, OTPSentType, SentOTPQuestion,
)
from woob.tools.capabilities.bank.investments import create_french_liquidity
from woob.tools.date import now_as_tz, now_as_utc
from woob.tools.misc import polling_loop
from woob_modules.caissedepargne.pages import VkImagePage
from woob_modules.linebourse.browser import LinebourseAPIBrowser

from .document_pages import BasicTokenPage, DocumentsPage, SubscriberPage, SubscriptionsPage
from .pages import (
    AccountsFullPage, AccountsNextPage, AccountsPage, AdvisorPage, AlreadyLoginPage, AppValidationPage,
    AuthenticationMethodPage, AuthenticationStepPage, AuthorizeErrorPage, AuthorizePage, BPCEPage,
    CaissedepargneVirtKeyboard, CardsPage, ErrorPage, EtnaPage, GenericAccountsPage, HomePage, IbanPage,
    IndexPage, InfoTokensPage, InvestmentPage, JsFilePage, LastConnectPage, LineboursePage, LoggedOut,
    Login2Page, LoginPage, LoginTokensPage, NatixisChoicePage, NatixisDetailsPage, NatixisErrorPage,
    NatixisHistoryPage, NatixisInvestPage, NatixisPage, NatixisRedirect, NatixisUnavailablePage,
    NewLoginPage, RedirectErrorPage, RedirectPage, TransactionDetailPage, TransactionsBackPage,
    TransactionsPage, UnavailableDocumentsPage, UnavailablePage,
)

__all__ = ['BanquePopulaire']


class BrokenPageError(Exception):
    pass


class TemporaryBrowserUnavailable(BrowserUnavailable):
    # To handle temporary errors that are usually solved just by making a retry
    pass


def retry(exc_check, tries=4):
    """Decorate a function to retry several times in case of exception.

    The decorated function is called at max 4 times. It is retried only when it
    raises an exception of the type `exc_check`.
    If the function call succeeds and returns an iterator, a wrapper to the
    iterator is returned. If iterating on the result raises an exception of type
    `exc_check`, the iterator is recreated by re-calling the function, but the
    values already yielded will not be re-yielded.
    For consistency, the function MUST always return values in the same order.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(browser, *args, **kwargs):
            def cb():
                return func(browser, *args, **kwargs)

            for i in range(tries, 0, -1):
                try:
                    ret = cb()
                except exc_check as exc:
                    browser.logger.debug('%s raised, retrying', exc)
                    continue

                if not (hasattr(ret, '__next__') or hasattr(ret, 'next')):
                    return ret  # simple value, no need to retry on items
                return iter_retry(cb, value=ret, remaining=i, exc_check=exc_check, logger=browser.logger)

            raise BrowserUnavailable('Site did not reply successfully after multiple tries')

        return wrapper
    return decorator


def no_need_login(func):
    # indicate a login is in progress, so LoggedOut should not be raised
    def wrapper(browser, *args, **kwargs):
        browser.no_login += 1
        try:
            return func(browser, *args, **kwargs)
        finally:
            browser.no_login -= 1

    return wrapper


class BanquePopulaire(TwoFactorBrowser):
    HTTP_ADAPTER_CLASS = LowSecHTTPAdapter

    TWOFA_DURATION = 90 * 24 * 60

    first_login_page = URL(r'/$')
    new_first_login_page = URL(r'/cyber/ibp/ate/portal/internet89C3Portal.jsp')
    login_page = URL(r'https://[^/]+/auth/UI/Login.*', LoginPage)
    new_login = URL(r'https://[^/]+/.*se-connecter/sso', NewLoginPage)
    js_file = URL(r'https://[^/]+/.*se-connecter/main\..*.js$', JsFilePage)
    authorize = URL(r'https://www.as-ex-ath-groupe.banquepopulaire.fr/api/oauth/v2/authorize', AuthorizePage)
    login_tokens = URL(r'https://www.as-ex-ath-groupe.banquepopulaire.fr/api/oauth/v2/consume', LoginTokensPage)
    info_tokens = URL(r'https://www.as-ex-ano-groupe.banquepopulaire.fr/api/oauth/token', InfoTokensPage)
    user_info = URL(
        r'https://www.rs-ex-ano-groupe.banquepopulaire.fr/bapi/user/v1/users/identificationRouting',
        InfoTokensPage
    )
    authentication_step = URL(
        r'https://www.icgauth.(?P<subbank>.*).fr/dacsrest/api/v1u0/transaction/(?P<validation_id>[^/]+)/step',
        AuthenticationStepPage
    )
    authentication_method_page = URL(
        r'https://www.icgauth.(?P<subbank>.*).fr/dacsrest/api/v1u0/transaction/(?P<validation_id>)',
        AuthenticationMethodPage,
    )
    vk_image = URL(
        r'https://www.icgauth.(?P<subbank>.*).fr/dacs-rest-media/api/v1u0/medias/mappings/[a-z0-9-]+/images',
        VkImagePage,
    )
    app_validation = URL(r'https://www.icgauth.(?P<subbank>.*).fr/dacsrest/WaitingCallbackHandler', AppValidationPage)
    index_page = URL(r'https://[^/]+/cyber/internet/Login.do', IndexPage)
    accounts_page = URL(
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=mesComptes.*',
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=maSyntheseGratuite.*',
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=accueilSynthese.*',
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=equipementComplet.*',
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=VUE_COMPLETE.*',
        AccountsPage
    )
    accounts_next_page = URL(r'https://[^/]+/cyber/internet/Page.do\?.*', AccountsNextPage)

    iban_page = URL(
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=cyberIBAN.*',
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=DETAIL_IBAN_RIB.*',
        IbanPage
    )

    accounts_full_page = URL(
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=EQUIPEMENT_COMPLET.*',
        AccountsFullPage
    )

    cards_page = URL(
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=ENCOURS_COMPTE.*',
        CardsPage
    )

    transactions_page = URL(
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=SELECTION_ENCOURS_CARTE.*',
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=SOLDE.*',
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=CONTRAT.*',
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=CANCEL.*',
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=BACK.*',
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=ordreBourseCTJ.*',
        r'https://[^/]+/cyber/internet/Page.do\?.*',
        r'https://[^/]+/cyber/internet/Sort.do\?.*',
        TransactionsPage
    )

    investment_page = URL(
        r'https://[^/]+/cyber/ibp/ate/skin/internet/pages/webAppReroutingAutoSubmit.jsp',
        InvestmentPage
    )

    transactions_back_page = URL(
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*ActionPerformed=BACK.*',
        TransactionsBackPage
    )

    transaction_detail_page = URL(
        r'https://[^/]+/cyber/internet/ContinueTask.do\?.*dialogActionPerformed=DETAIL_ECRITURE.*',
        TransactionDetailPage
    )

    error_page = URL(
        r'https://[^/]+/cyber/internet/ContinueTask.do',
        r'https://[^/]+/_layouts/error.aspx',
        r'https://[^/]+/portailinternet/_layouts/Ibp.Cyi.Administration/RedirectPageError.aspx',
        ErrorPage
    )

    unavailable_page = URL(
        r'https://[^/]+/s3f-web/.*',
        r'https://[^/]+/static/errors/nondispo.html',
        r'/i-RIA/swc/1.0.0/desktop/index.html',
        UnavailablePage
    )

    authorize_error = URL(r'https://[^/]+/dacswebssoissuer/AuthnRequestServlet', AuthorizeErrorPage)

    redirect_page = URL(r'https://[^/]+/portailinternet/_layouts/Ibp.Cyi.Layouts/RedirectSegment.aspx.*', RedirectPage)
    bpce_page = URL(r'https://[^/]+/cyber/ibp/ate/portal/internet89C3Portal.jsp', BPCEPage)

    redirect_error_page = URL(
        r'https://[^/]+/portailinternet/?$',
        RedirectErrorPage
    )

    home_page = URL(
        r'https://[^/]+/portailinternet/Catalogue/Segments/.*.aspx(\?vary=(?P<vary>.*))?',
        r'https://[^/]+/portailinternet/Pages/.*.aspx\?vary=(?P<vary>.*)',
        r'https://[^/]+/portailinternet/Pages/[dD]efault.aspx',
        r'https://[^/]+/portailinternet/Transactionnel/Pages/CyberIntegrationPage.aspx',
        r'https://[^/]+/cyber/internet/ShowPortal.do\?token=.*',
        r'https://[^/]+/cyber/internet/ShowPortal.do\?taskInfoOID=.*',
        HomePage
    )

    already_login_page = URL(
        r'https://[^/]+/dacswebssoissuer.*',
        r'https://[^/]+/WebSSO_BP/_(?P<bankid>\d+)/index.html\?transactionID=(?P<transactionID>.*)',
        AlreadyLoginPage
    )
    login2_page = URL(
        r'https://[^/]+/WebSSO_BP/_(?P<bankid>\d+)/index.html\?transactionID=(?P<transactionID>.*)',
        Login2Page
    )

    # natixis
    natixis_redirect = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/views/common/routage.xhtml.*?dswid=-?[a-f0-9]+$',
        NatixisRedirect
    )
    natixis_choice = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/views/contrat/list.xhtml\?.*',
        NatixisChoicePage
    )
    natixis_page = URL(r'https://www.assurances.natixis.fr/espaceinternet-bp/views/common.*', NatixisPage)
    etna = URL(
        r'https://www.assurances.natixis.fr/etna-ihs-bp/#/contratVie/(?P<id1>\w+)/(?P<id2>\w+)/(?P<id3>\w+).*',
        r'https://www.assurances.natixis.fr/espaceinternet-bp/views/contrat/detail/vie/view.xhtml\?windowId=.*&reference=(?P<id3>\d+)&codeSociete=(?P<id1>[^&]*)&codeProduit=(?P<id2>[^&]*).*',
        EtnaPage
    )
    natixis_error_page = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/error-redirect.*',
        r'https://www.assurances.natixis.fr/etna-ihs-bp/#/equipement;codeEtab=.*\?windowId=.*',
        NatixisErrorPage
    )
    natixis_unavailable_page = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/page500.xhtml',
        NatixisUnavailablePage
    )
    natixis_invest = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/rest/v2/contratVie/load/(?P<id1>\w+)/(?P<id2>\w+)/(?P<id3>\w+)',
        NatixisInvestPage
    )
    natixis_history = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/rest/v2/contratVie/load-operation/(?P<id1>\w+)/(?P<id2>\w+)/(?P<id3>\w+)',
        NatixisHistoryPage
    )
    natixis_pdf = URL(
        r'https://www.assurances.natixis.fr/espaceinternet-bp/rest/v2/contratVie/load-releve/(?P<id1>\w+)/(?P<id2>\w+)/(?P<id3>\w+)/(?P<year>\d+)',
        NatixisDetailsPage
    )

    linebourse_home = URL(r'https://www.linebourse.fr', LineboursePage)

    advisor = URL(
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=accueil.*',
        r'https://[^/]+/cyber/internet/StartTask.do\?taskInfoOID=contacter.*',
        AdvisorPage
    )

    basic_token_page = URL(r'https://(?P<website>.[\w\.]+)/SRVATE/context/mde/1.1.5', BasicTokenPage)
    subscriber_page = URL(r'/api-bp/wapi/2.0/abonnes/current/mes-documents-electroniques', SubscriberPage)
    subscription_page = URL(r'https://[^/]+/api-bp/wapi/2.0/abonnes/current2/contrats', SubscriptionsPage)
    documents_page = URL(r'/api-bp/wapi/2.0/abonnes/current/documents/recherche-avancee', DocumentsPage)

    last_connect = URL(
        r'https://www.rs-ex-ath-groupe.banquepopulaire.fr/bapi/user/v1/user/lastConnect',
        LastConnectPage
    )

    redirect_uri = URL(r'/callback')

    current_subbank = None

    HAS_CREDENTIALS_ONLY = True

    def __init__(self, website, config, *args, **kwargs):
        self.website = website
        self.config = config
        super(BanquePopulaire, self).__init__(
            self.config, self.config['login'].get(), self.config['password'].get(), *args, **kwargs
        )
        self.BASEURL = 'https://%s' % self.website
        self.is_creditmaritime = 'cmgo.creditmaritime' in self.BASEURL
        self.validation_id = None
        self.mfa_validation_data = None
        self.user_type = None
        self.cdetab = None
        self.continue_url = None
        self.current_subbank = None
        self.term_id = None
        self.access_token = None

        if self.is_creditmaritime:
            # this url is required because the creditmaritime abstract uses an other url
            self.redirect_url = 'https://www.icgauth.creditmaritime.groupe.banquepopulaire.fr/dacsrest/api/v1u0/transaction/'
        else:
            self.redirect_url = 'https://www.icgauth.banquepopulaire.fr/dacsrest/api/v1u0/transaction/'
        self.token = None

        dirname = self.responses_dirname
        if dirname:
            dirname += '/bourse'
        self.linebourse = LinebourseAPIBrowser(
            'https://www.linebourse.fr',
            logger=self.logger,
            responses_dirname=dirname,
            proxy=self.PROXIES
        )

        self.documents_headers = None

        self.AUTHENTICATION_METHODS = {
            'code_sms': self.handle_sms,
            'code_emv': self.handle_emv,
            'resume': self.handle_cloudcard,
        }

        self.__states__ += (
            'validation_id',
            'mfa_validation_data',
            'user_type',
            'cdetab',
            'continue_url',
            'current_subbank',
            'term_id',
            'user_code',
        )

    def deinit(self):
        super(BanquePopulaire, self).deinit()
        self.linebourse.deinit()

    no_login = 0

    def follow_back_button_if_any(self, params=None, actions=None):
        """
        Look for a Retour button and follow it using a POST
        :param params: Optional form params to use (default: call self.page.get_params())
        :param actions: Optional actions to use (default: call self.page.get_button_actions())
        :return: None
        """
        if not self.page:
            return

        data = self.page.get_back_button_params(params=params, actions=actions)
        if data:
            self.location('/cyber/internet/ContinueTask.do', data=data)

    def load_state(self, state):
        if state.get('validation_unit'):
            # If starting in the middle of a 2FA, and calling for a new authentication_method_page,
            # we'll lose validation_unit validity.
            state.pop('url', None)
        super(BanquePopulaire, self).load_state(state)

    def locate_browser(self, state):
        if self.index_page.match(state.get('url', '')):
            # the URL of the index page leads to an error page.
            return
        super(BanquePopulaire, self).locate_browser(state)

    def init_login(self):
        if (
            self.twofa_logged_date and (
                now_as_utc() > (self.twofa_logged_date + timedelta(minutes=self.TWOFA_DURATION))
            )
        ):
            # Since we doing a PUT at every login, we assume that the 2FA of banquepopulaire as no duration
            # Reseting after 90 days because of legal concerns
            self.term_id = None

        if not self.term_id:
            # The term_id is the terminal id
            # It bounds a terminal to a valid two factor authentication
            # If not present, we are generating one
            self.term_id = str(uuid4())

        try:
            self.new_first_login_page.go()
        except (ClientError, HTTPNotFound) as e:
            if e.response.status_code in (403, 404):
                # Sometimes the website makes some redirections that leads
                # to a 404 or a 403 when we try to access the BASEURL
                # (website is not stable).
                raise BrowserUnavailable(str(e))
            raise

        # avoids trying to relog in while it's already on home page
        if self.home_page.is_here():
            return

        if self.new_login.is_here():
            self.do_new_login()
        else:
            self.do_old_login()

        if self.authentication_step.is_here():
            # We are successfully logged in with a 2FA still valid
            if self.page.is_authentication_successful():
                self.validation_id = None  # Don't want an old validation_id in storage.
                self.finalize_login()
                return

            self.page.check_errors(feature='login')

            auth_method = self.page.get_authentication_method_type()
            self.get_current_subbank()
            self._set_mfa_validation_data()

            if auth_method == 'SMS':
                phone_number = self.page.get_phone_number()
                raise SentOTPQuestion(
                    'code_sms',
                    medium_type=OTPSentType.SMS,
                    message='Veuillez entrer le code reçu au numéro %s' % phone_number,
                )
            elif auth_method == 'CLOUDCARD':
                # At that point notification has already been sent, although
                # the website displays a button to chose another auth method.
                devices = self.page.get_devices()
                if not len(devices):
                    raise AssertionError('Found no device, please audit')
                if len(devices) > 1:
                    raise AssertionError('Found several devices, please audit to implement choice')

                # name given at the time of device enrolling done in the bank's app, empty name is not allowed
                device_name = devices[0]['friendlyName']

                # Time seen and tested: 540" = 9'.
                # At the end of that duration, we can still validate in the app, but a message is then displayed: "Opération déjà refusée".
                # In a navigator, website displays "Votre session a expiré" and propose to log in again.
                expires_at = now_as_utc() + timedelta(seconds=self.page.get_time_left())
                raise AppValidation(
                    message=f"Prenez votre téléphone «{device_name}»."
                    + " Ouvrez votre application mobile."
                    + " Saisissez votre code Sécur'Pass sur le téléphone,"
                    + " ou utilisez votre identification biométrique.",
                    expires_at=expires_at,
                    medium_label=device_name,
                )
            else:
                raise AssertionError('Unhandled authentication method: %s' % auth_method)
        raise AssertionError('Did not encounter authentication_step page after performing the login')

    def handle_2fa_otp(self, otp_type):
        # It will occur when states become obsolete
        if not self.mfa_validation_data:
            raise BrowserIncorrectPassword('Le délai pour saisir le code a expiré, veuillez recommencer')

        data = {
            'validate': {
                self.mfa_validation_data['validation_unit_id']: [{
                    'id': self.mfa_validation_data['id'],
                }],
            },
        }

        data_otp = data['validate'][self.mfa_validation_data['validation_unit_id']][0]
        data_otp['type'] = otp_type
        if otp_type == 'SMS':
            data_otp['otp_sms'] = self.code_sms
        elif otp_type == 'EMV':
            data_otp['token'] = self.code_emv

        try:
            self.authentication_step.go(
                subbank=self.current_subbank,
                validation_id=self.validation_id,
                json=data
            )
        except (ClientError, ServerError) as e:
            if (
                # "Session Expired" seems to be a 500, this is strange because other OTP errors are 400
                e.response.status_code in (400, 500)
                and 'error' in e.response.json()
                and e.response.json()['error'].get('code', '') in (104, 105, 106)
            ):
                # Sometimes, an error message is displayed to user :
                # - '{"error":{"code":104,"message":"Unknown validation unit ID"}}'
                # - '{"error":{"code":105,"message":"No session found"}}'
                # - '{"error":{"code":106,"message":"Session Expired"}}'
                # So we give a clear message and clear 'auth_data' to begin from the top next time.
                self.authentification_data = {}
                raise BrowserIncorrectPassword('Votre identification par code a échoué, veuillez recommencer')
            raise

        self.mfa_validation_data = None

        authentication_status = self.page.authentication_status()
        if authentication_status == 'AUTHENTICATION_SUCCESS':
            self.validation_id = None  # Don't want an old validation_id in storage.
            self.finalize_login()
        else:
            self.page.login_errors(authentication_status, otp_type=otp_type)

    def handle_sms(self):
        self.handle_2fa_otp(otp_type='SMS')

    def handle_emv(self):
        self.handle_2fa_otp(otp_type='EMV')

    def handle_cloudcard(self, **params):
        assert self.mfa_validation_data

        for _ in polling_loop(timeout=300, delay=5):
            self.app_validation.go(subbank=self.current_subbank)
            status = self.page.get_status()

            # The status is 'valid' even for non success authentication
            # But authentication status is checked in authentication_step response.
            # Ex: when the user refuses the authentication on the application, AUTHENTICATION_CANCELED is returned.
            if status == 'valid':
                self.authentication_step.go(
                    subbank=self.current_subbank,
                    validation_id=self.validation_id,
                    json={
                        'validate': {
                            self.mfa_validation_data['validation_unit_id']: [{
                                'id': self.mfa_validation_data['id'],
                                'type': 'CLOUDCARD',
                            }],
                        },
                    },
                )
                authentication_status = self.page.authentication_status()
                if authentication_status == 'AUTHENTICATION_SUCCESS':
                    self.finalize_login()
                    self.validation_id = None
                    self.mfa_validation_data = None
                    break
                else:
                    self.page.check_errors(feature='login')

            assert status == 'progress', 'Unhandled CloudCard status : "%s"' % status

        else:
            self.validation_id = None
            self.mfa_validation_data = None
            raise AppValidationExpired()

    def do_old_login(self):
        assert self.login2_page.is_here(), 'Should be on login2 page'
        self.page.set_form_ids()

        try:
            self.page.login(self.username, self.password)
        except BrowserUnavailable as ex:
            # HACK: some accounts with legacy password fails (legacy means not only digits).
            # The website crashes, even on a web browser.
            # So, if we get a specific exception AND if we have a legacy password,
            # we raise WrongPass instead of BrowserUnavailable.
            if 'Cette page est indisponible' in str(ex) and not self.password.isdigit():
                raise BrowserIncorrectPassword()
            raise
        if not self.password.isnumeric():
            self.logger.warning('Password with non numeric chararacters still works')

        if self.login_page.is_here():
            raise BrowserIncorrectPassword()
        if 'internetRescuePortal' in self.url:
            # 1 more request is necessary
            data = {'integrationMode': 'INTERNET_RESCUE'}
            self.location('/cyber/internet/Login.do', data=data)

    def get_bpcesta(self):
        return {
            'csid': str(uuid4()),
            'typ_app': 'rest',
            'enseigne': 'bp',
            'typ_sp': 'out-band',
            'typ_act': 'auth',
            'snid': '6782561',
            'cdetab': self.cdetab,
            'typ_srv': self.user_type,
            'term_id': self.term_id,
        }

    def _set_mfa_validation_data(self):
        """Same as in caissedepargne."""
        self.mfa_validation_data = self.page.get_authentication_method_info()
        self.mfa_validation_data['validation_unit_id'] = self.page.validation_unit_id

    # need to try from the top in that case because this login is a long chain of redirections
    @retry(TemporaryBrowserUnavailable)
    def do_new_login(self):
        # Same login as caissedepargne
        url_params = parse_qs(urlparse(self.url).query)
        self.cdetab = url_params['cdetab'][0]
        self.continue_url = url_params['continue'][0]

        main_js_file = self.page.get_main_js_file_url()
        self.location(main_js_file)

        client_id = self.page.get_client_id()
        nonce = str(uuid4())  # Not found anymore

        data = {
            'grant_type': 'client_credentials',
            'client_id': self.page.get_user_info_client_id(),
            'scope': '',
        }

        # The 2 followings requests are needed in order to get
        # user type (part, ent and pro)
        self.info_tokens.go(data=data)

        headers = {'Authorization': 'Bearer %s' % self.page.get_access_token()}
        data = {
            'characteristics': {
                'iTEntityType': {
                    'code': '03',  # 03 for BP and 02 for CE
                    'label': 'BP',
                },
                # banquepopulaire front allows lower or upper username but it needs to be capitalize for making request
                # If we don't, at the end of the login we will have an error that let us think the account if locked.
                'userCode': self.username.upper(),
                'bankId': self.cdetab,
                'subscribeTypeItems': [],
            },
        }
        try:
            self.user_info.go(headers=headers, json=data)
        except ReadTimeout:
            # This server usually delivers data in less than a second on this request.
            # If it timeouts, retrying will not help.
            # It usually comes back up within a few hours.

            raise BrowserUnavailable('Le service est momentanément indisponible. Veuillez réessayer plus tard.')

        self.user_type = self.page.get_user_type()
        # Recovering the username here and use it later is mandatory:
        # - banquepopulaire front allows lower or upper username but it needs to be capitalize for making request
        # - For palatine users, "1234567" username become "K1234567P00"
        # If we don't use it in further requests, at the end of the login we will have an error that let us think the account if locked.
        self.user_code = self.page.get_user_code()

        bpcesta = self.get_bpcesta()

        if self.user_type == 'pro':
            is_pro = True
        else:
            is_pro = None

        claims = {
            'userinfo': {
                'cdetab': None,
                'authMethod': None,
                'authLevel': None,
            },
            'id_token': {
                'auth_time': {
                    'essential': True,
                },
                'last_login': None,
                'cdetab': None,
                'pro': is_pro,
            },
        }

        params = {
            'cdetab': self.cdetab,
            'client_id': client_id,
            'response_type': 'id_token token',
            'nonce': nonce,
            'response_mode': 'form_post',
            'redirect_uri': self.redirect_uri.build(),
            'claims': json.dumps(claims),
            'bpcesta': json.dumps(bpcesta),
            'login_hint': self.user_code,
            'phase': '',
            'display': 'page',
        }
        headers = {
            'Accept': 'application/json, text/plain, */*',  # Mandatory, else you've got an HTML page.
            'Content-Type': 'application/x-www-form-urlencoded',
            'Content-Length': '0',  # Mandatory, otherwhise enjoy the 415 error
        }

        self.authorize.go(params=params, method='POST', headers=headers)
        self.do_redirect('SAMLRequest')
        self.validation_id = self.page.get_validation_id()
        self.get_current_subbank()

        security_level = self.page.get_security_level()
        is_sca_expected = self.page.is_sca_expected()

        # It means we are going to encounter an SCA
        if is_sca_expected:
            self.check_interactive()

        auth_method = self.check_for_fallback()
        if auth_method == 'CERTIFICATE':
            raise AuthMethodNotImplemented("La méthode d'authentification par certificat n'est pas gérée")
        elif auth_method == 'EMV':
            # This auth method replaces the sequence PASSWORD+SMS.
            # So we are on authentication_method_page.
            self._set_mfa_validation_data()
            raise OfflineOTPQuestion(
                'code_emv',
                message='Veuillez renseigner le code affiché sur le boitier (Pass Cyberplus en mode « Code »)',
            )

        if self.authorize_error.is_here():
            raise BrowserUnavailable(self.page.get_error_message())
        self.page.check_errors(feature='login')
        validation_unit = self.page.validation_unit_id

        vk_info = self.page.get_authentication_method_info()
        vk_id = vk_info['id']

        if vk_info.get('virtualKeyboard') is None:
            # no VK, password to submit
            code = self.password
        else:
            if not self.password.isnumeric():
                raise BrowserIncorrectPassword('Le mot de passe doit être composé de chiffres uniquement')

            vk_images_url = vk_info['virtualKeyboard']['externalRestMediaApiUrl']

            self.location(vk_images_url)
            images_url = self.page.get_all_images_data()
            vk = CaissedepargneVirtKeyboard(self, images_url)
            code = vk.get_string_code(self.password)

        headers = {
            'Referer': self.BASEURL,
            'Accept': 'application/json, text/plain, */*',
        }

        self.authentication_step.go(
            subbank=self.current_subbank,
            validation_id=self.validation_id,
            json={
                'validate': {
                    validation_unit: [{
                        'id': vk_id,
                        'password': code,
                        'type': 'PASSWORD',
                    }],
                },
            },
            headers=headers,
        )

        if self.authentication_step.is_here():
            status = self.page.get_status()
            if status == 'AUTHENTICATION_SUCCESS':
                self.logger.warning("Security level %s is not linked to an SCA", security_level)
            elif status == 'AUTHENTICATION':
                auth_method = self.page.get_authentication_method_type()
                if auth_method:
                    self.logger.warning(
                        "Security level %s is linked to an SCA with %s auth method",
                        security_level, auth_method
                    )
            else:
                self.logger.warning(
                    "Encounter %s security level without authentication success and any auth method",
                    security_level
                )

    @retry(BrokenPageError, tries=2)
    def handle_continue_url(self):
        # continueURL not found in HAR
        params = {
            'Segment': self.user_type,
            'NameId': self.user_code,
            'cdetab': self.cdetab,
            'continueURL': '/cyber/ibp/ate/portal/internet89C3Portal.jsp?taskId=aUniversAccueilRefonte',
        }

        self.location(self.continue_url, params=params)
        if self.response.status_code == 302:
            # No redirection to the next url
            # Let's do the job instead of the bank
            self.location('/portailinternet')

        if self.new_login.is_here():
            # Sometimes, we land on the wrong page. If we retry, it usually works.
            raise BrokenPageError()

    def finalize_login(self):
        headers = {
            'Referer': self.BASEURL,
            'Accept': 'application/json, text/plain, */*',
        }

        self.page.check_errors(feature='login')
        self.do_redirect('SAMLResponse', headers)

        self.handle_continue_url()

        url_params = parse_qs(urlparse(self.url).query)
        validation_id = url_params['transactionID'][0]

        self.authentication_method_page.go(
            subbank=self.current_subbank, validation_id=validation_id
        )
        # Need to do the redirect a second time to finish login
        self.do_redirect('SAMLResponse', headers)

        self.put_terminal_id()

    def check_for_fallback(self):
        for _ in range(3):
            current_method = self.page.get_authentication_method_type()
            if self.page.is_other_authentication_method() and current_method != 'PASSWORD':
                # we might first have a CERTIFICATE method, which we may fall back to EMV,
                # which we may fall back to PASSWORD
                self.authentication_step.go(
                    subbank=self.current_subbank,
                    validation_id=self.validation_id,
                    json={'fallback': {}},
                )
            else:
                break
        return current_method

    ACCOUNT_URLS = ['mesComptes', 'mesComptesPRO', 'maSyntheseGratuite', 'accueilSynthese', 'equipementComplet']

    def do_redirect(self, keyword, headers=None):
        if headers is None:
            headers = {}

        # During the second do_redirect
        # The AuthenticationMethodPage carries a status response
        # This status can be different from AUTHENTICATION_SUCCESS
        # Even if the do_new_login flow went well
        # (Yes, even if the status response in do_new_login was AUTHENTICATION_SUCCESS.....)

        if self.authentication_method_page.is_here():
            self.page.check_errors(feature='login')
        next_url = self.page.get_next_url()
        payload = self.page.get_payload()
        self.location(next_url, data={keyword: payload}, headers=headers)

        if self.login_tokens.is_here():
            self.access_token = self.page.get_access_token()

            if self.access_token is None:
                raise AssertionError('Did not obtain the access_token mandatory to finalize the login')

        if self.redirect_error_page.is_here() and self.page.is_unavailable():
            # website randomly unavailable, need to retry login from the beginning
            self.do_logout()  # will delete cookies, or we'll always be redirected here
            self.location(self.BASEURL)
            raise TemporaryBrowserUnavailable()

    def put_terminal_id(self):
        # This request is mandatory.
        # We assume it associates the current terminal_id,
        # generate at the beginning of the login,
        # to the SCA that has been validated.
        # Presenting this terminal_id for further login
        # will avoid triggering another SCA.

        # This request occurs at every login on
        # banquepopulaire website
        # To ensure consistency, we are doing so.
        self.last_connect.go(
            method='PUT',
            headers={
                'Authorization': 'Bearer %s' % self.access_token,
                'X-Id-Terminal': self.term_id,
            },
            json={}
        )

    @retry(BrokenPageError)
    @need_login
    def go_on_accounts_list(self):
        for taskInfoOID in self.ACCOUNT_URLS:
            # 4 possible URLs but we stop as soon as one of them works
            # Some of the URL are sometimes temporarily unavailable
            # so we handle potential errors and continue to the next
            # iteration of the loop if that's the case to get the next URL
            data = OrderedDict([('taskInfoOID', taskInfoOID), ('token', self.token)])

            # Go from AdvisorPage to AccountsPage
            try:
                self.location(self.absurl('/cyber/internet/StartTask.do', base=True), params=data)
            except ServerError as e:
                if e.response.status_code == 500 and 'votre demande ultérieurement' in e.response.text:
                    continue
                raise

            if "indisponible pour cause de maintenance." in self.response.text:
                continue

            if not self.page.is_error():
                if self.page.pop_up():
                    self.logger.debug('Popup displayed, retry')
                    data = OrderedDict([('taskInfoOID', taskInfoOID), ('token', self.token)])
                    self.location('/cyber/internet/StartTask.do', params=data)

                # Set the valid ACCOUNT_URL and break the loop
                self.ACCOUNT_URLS = [taskInfoOID]
                break
        else:
            raise BrokenPageError('Unable to go on the accounts list page')

        if self.page.is_short_list():
            # Go from AccountsPage to AccountsFullPage to get the full accounts list
            form = self.page.get_form(nr=0)
            form['dialogActionPerformed'] = 'EQUIPEMENT_COMPLET'
            form['token'] = self.page.build_token(form['token'])
            form.submit()

        # In case of prevAction maybe we have reached an expanded accounts list page, need to go back
        self.follow_back_button_if_any()

    def get_loan_from_account(self, account):
        loan = Loan.from_dict(account.to_dict())
        loan._prev_debit = account._prev_debit
        loan._next_debit = account._next_debit
        loan._params = account._params
        loan._coming_params = account._coming_params
        loan._coming_count = account._coming_count
        loan._invest_params = account._invest_params
        loan._loan_params = account._loan_params

        # if the loan is fully refunded we avoid sending the form,
        # it seems that there is no more detail, so server is reponding 400
        # avoid calling detail when it is a leasing type of contract because it must be handled differently
        if (
            account._invest_params
            and 'mesComptes' in account._invest_params['taskInfoOID']
            and 'Credit Bail' not in account.label
        ):
            form = self.page.get_form(id='myForm')
            form.update(account._invest_params)
            form['token'] = self.page.get_params()['token']
            form.submit()
            self.page.fill_loan(obj=loan)
            self.follow_back_button_if_any()

        return loan

    @retry(LoggedOut)
    @need_login
    def iter_accounts(self, get_iban=True):
        # We have to parse account list in 2 different way depending if
        # we want the iban number or not thanks to stateful website
        next_pages = []
        accounts = []
        owner_type = self.get_owner_type()
        profile = self.get_profile()

        if profile:
            if profile.name:
                name = profile.name
            else:
                name = profile.company_name

            # Handle names/company names without spaces
            if ' ' in name:
                owner_name = re.search(r' (.+)', name).group(1).upper()
            else:
                owner_name = name.upper()
        else:
            # AdvisorPage is not available for all users
            owner_name = None

        self.go_on_accounts_list()

        for account in self.page.iter_accounts(next_pages):
            account.owner_type = owner_type
            if owner_name:
                self.set_account_ownership(account, owner_name)

            if account.type in (Account.TYPE_LOAN, Account.TYPE_MORTGAGE):
                account = self.get_loan_from_account(account)

            accounts.append(account)
            if not get_iban:
                yield account

        while len(next_pages) > 0:
            next_with_params = None
            next_page = next_pages.pop()

            if not self.accounts_full_page.is_here():
                self.go_on_accounts_list()
            # If there is an action needed to go to the "next page", do it.
            if 'prevAction' in next_page:
                params = self.page.get_params()
                params['dialogActionPerformed'] = next_page.pop('prevAction')
                params['token'] = self.page.build_token(self.token)
                self.location('/cyber/internet/ContinueTask.do', data=params)

            # Go to next_page with params and token
            next_page['token'] = self.page.build_token(self.token)
            try:
                self.location('/cyber/internet/ContinueTask.do', data=next_page)
            except ServerError as e:
                if e.response.status_code == 500 and 'votre demande ultérieurement' in e.response.text:
                    continue
                raise
            secure_iteration = 0
            while secure_iteration == 0 or (next_with_params and secure_iteration < 10):
                # The first condition allows to do iter_accounts with less than 20 accounts
                secure_iteration += 1
                # If we have more than 20 accounts of a type
                # The next page is reached by params found in the current page
                if isinstance(self.page, GenericAccountsPage):
                    next_with_params = self.page.get_next_params()
                else:
                    # Can be ErrorPage
                    next_with_params = None

                accounts_iter = self.page.iter_accounts(
                    next_pages, accounts_parsed=accounts,
                    next_with_params=next_with_params
                )
                for a in accounts_iter:
                    a.owner_type = owner_type
                    self.set_account_ownership(a, owner_name)
                    if a.type in (Account.TYPE_LOAN, Account.TYPE_MORTGAGE):
                        a = self.get_loan_from_account(a)
                    accounts.append(a)
                    if not get_iban:
                        yield a

                if next_with_params:
                    self.location('/cyber/internet/Page.do', params=next_with_params)

        if get_iban:
            for a in accounts:
                a.owner_type = owner_type
                a.iban = self.get_iban_number(a)
                yield a

    # TODO: see if there's other type of account with a label without name which
    # is not ATTORNEY (cf. 'COMMUN'). Didn't find one right now.
    def set_account_ownership(self, account, owner_name):
        if not account.ownership:
            label = account.label.upper()
            if account.parent:
                if not account.parent.ownership:
                    self.set_account_ownership(account.parent, owner_name)
                account.ownership = account.parent.ownership
            elif owner_name in label:
                if (
                    re.search(
                        r'(m|mr|me|mme|mlle|mle|ml)\.? (.*)\bou (m|mr|me|mme|mlle|mle|ml)\b(.*)',
                        label, re.IGNORECASE)
                ):
                    account.ownership = AccountOwnership.CO_OWNER
                else:
                    account.ownership = AccountOwnership.OWNER
            elif 'COMMUN' in label:
                account.ownership = AccountOwnership.CO_OWNER
            else:
                account.ownership = AccountOwnership.ATTORNEY

    @need_login
    def get_iban_number(self, account):
        # Some rare users have no IBAN available at all on the whole account
        # triggering a ServerError 500 if we try to access the IBAN page
        try:
            url = self.absurl(
                '/cyber/internet/StartTask.do?taskInfoOID=cyberIBAN&token=%s' % self.page.build_token(self.token),
                base=True
            )
            self.location(url)
        except ServerError as e:
            if e.response.status_code == 500 and 'Votre abonnement ne vous permet pas' in e.response.text:
                return NotAvailable
            raise
        # Sometimes we can't choose an account
        if (
            account.type in (Account.TYPE_LIFE_INSURANCE, Account.TYPE_MARKET)
            or (self.page.need_to_go() and not self.page.go_iban(account))
        ):
            return NotAvailable
        return self.page.get_iban(account.id)

    @retry(LoggedOut)
    @need_login
    def get_account(self, id):
        return find_object(self.iter_accounts(get_iban=False), id=id)

    def set_gocardless_transaction_details(self, transaction):
        # Setting references for a GoCardless transaction
        data = self.page.get_params()
        data['validationStrategy'] = self.page.get_gocardless_strategy_param(transaction)
        data['dialogActionPerformed'] = 'DETAIL_ECRITURE'
        attribute_key, attribute_value = self.page.get_transaction_table_id(transaction._ref)
        data[attribute_key] = attribute_value
        data['token'] = self.page.build_token(data['token'])

        self.location(self.absurl('/cyber/internet/ContinueTask.do', base=True), data=data)
        ref = self.page.get_reference()
        transaction.raw = '%s %s' % (transaction.raw, ref)

        # Needed to preserve navigation.
        self.follow_back_button_if_any()

    @retry(LoggedOut)
    @need_login
    def iter_history(self, account, coming=False):
        def get_history_by_receipt(account, coming, sel_tbl1=None):
            account = self.get_account(account.id)

            if account is None:
                raise BrowserUnavailable()

            if account._invest_params or (account.id.startswith('TIT') and account._params):
                if not coming:
                    for tr in self.get_invest_history(account):
                        yield tr
                return

            if coming:
                params = account._coming_params
            else:
                params = account._params

            if params is None:
                return
            params['token'] = self.page.build_token(params['token'])

            if sel_tbl1 is not None:
                params['attribute($SEL_$tbl1)'] = str(sel_tbl1)

            self.location(self.absurl('/cyber/internet/ContinueTask.do', base=True), data=params)

            if not self.page or self.error_page.is_here() or self.page.no_operations():
                return

            # Sort by operation date
            if len(self.page.doc.xpath('//a[@id="tcl4_srt"]')) > 0:
                # The first request sort might transaction by oldest. If this is the case,
                # we need to do the request a second time for the transactions to be sorted by newest.
                for _ in range(2):
                    form = self.page.get_form(id='myForm')
                    form.url = self.absurl('/cyber/internet/Sort.do?property=tbl1&sortBlocId=blc2&columnName=dateOperation')
                    params['token'] = self.page.build_token(params['token'])
                    form.submit()
                    if self.page.is_sorted_by_most_recent():
                        break

            transactions_next_page = True

            while transactions_next_page:
                assert self.transactions_page.is_here()

                transaction_list = self.page.get_history(account, coming)

                for tr in transaction_list:
                    # Add information about GoCardless
                    if 'GoCardless' in tr.label and tr._has_link:
                        self.set_gocardless_transaction_details(tr)
                    yield tr

                next_params = self.page.get_next_params()
                # Go to the next transaction page only if it exists:
                if next_params is None:
                    transactions_next_page = False
                else:
                    self.location('/cyber/internet/Page.do', params=next_params)

        if coming and account._coming_count:
            for i in range(account._coming_start, account._coming_start + account._coming_count):
                for tr in get_history_by_receipt(account, coming, sel_tbl1=i):
                    yield tr
        else:
            for tr in get_history_by_receipt(account, coming):
                yield tr

    @need_login
    def go_investments(self, account, get_account=False):
        if not account._invest_params and not (account.id.startswith('TIT') or account.id.startswith('PRV')):
            raise NotImplementedError()

        if get_account:
            account = self.get_account(account.id)

        if account._params:
            params = {
                'taskInfoOID': 'ordreBourseCTJ2',
                'controlPanelTaskAction': 'true',
                'token': self.page.build_token(account._params['token']),
            }
            self.location(self.absurl('/cyber/internet/StartTask.do', base=True), params=params)
            try:
                # Form to complete the user's info, we can pass it
                form = self.page.get_form()
                if 'QuestConnCliInt.EcranMessage' in form.get('screenName', ''):
                    form['dialogActionPerformed'] = 'CANCEL'
                    form['validationStrategy'] = 'NV'
                    form.submit()
            except FormNotFound:
                pass
        else:
            params = account._invest_params
            params['token'] = self.page.build_token(params['token'])
            try:
                self.location(self.absurl('/cyber/internet/ContinueTask.do', base=True), data=params)
            except BrowserUnavailable:
                return False

        if self.error_page.is_here():
            raise NotImplementedError()

        if self.page.go_investment():
            url, params = self.page.get_investment_page_params()
            if params:
                try:
                    self.location(url, data=params)
                except BrowserUnavailable:
                    return False

                if 'linebourse' in self.url:
                    self.linebourse.session.cookies.update(self.session.cookies)
                    self.linebourse.session.headers['X-XSRF-TOKEN'] = self.session.cookies.get('XSRF-TOKEN')

                if self.natixis_error_page.is_here():
                    self.logger.warning('Natixis site does not work.')
                    return False

                if self.natixis_redirect.is_here():
                    url = self.page.get_redirect()
                    if (
                        re.match(
                            r'https://www.assurances.natixis.fr/etna-ihs-bp/#/equipement;codeEtab=\d+\?windowId=[a-f0-9]+$',
                            url)
                    ):
                        self.logger.warning('There may be no contract associated with %s, skipping', url)
                        return False
        return True

    @need_login
    def iter_investments(self, account):
        if account.type not in (Account.TYPE_LIFE_INSURANCE, Account.TYPE_PEA, Account.TYPE_MARKET, Account.TYPE_PERP):
            return

        # Add "Liquidities" investment if the account is a "Compte titres PEA":
        if account.type == Account.TYPE_PEA and account.id.startswith('CPT'):
            yield create_french_liquidity(account.balance)
            return
        if self.go_investments(account, get_account=True):
            # Redirection URL is https://www.linebourse.fr/ReroutageSJR
            if 'linebourse' in self.url:
                self.logger.warning('Going to Linebourse space to fetch investments.')
                # Eliminating the 3 letters prefix to match IDs on Linebourse:
                linebourse_id = account.id[3:]
                for inv in self.linebourse.iter_investments(linebourse_id):
                    yield inv
                return

            if self.etna.is_here():
                self.logger.warning('Going to Etna space to fetch investments.')
                params = self.page.params

            elif self.natixis_redirect.is_here():
                self.logger.warning('Going to Natixis space to fetch investments.')
                # the url may contain a "#", so we cannot make a request to it, the params after "#" would be dropped
                url = self.page.get_redirect()
                self.logger.debug('using redirect url %s', url)
                m = self.etna.match(url)
                if not m:
                    # URL can be contratPrev which is not investments
                    self.logger.warning('Unable to handle this kind of contract.')
                    return

                params = m.groupdict()

            if self.natixis_redirect.is_here() or self.etna.is_here():
                try:
                    self.natixis_invest.go(**params)
                except ServerError:
                    # Broken website... nothing to do.
                    return
                if self.natixis_unavailable_page.is_here():
                    raise BrowserUnavailable(self.page.get_message())
                for inv in self.page.iter_investments():
                    yield inv

    @need_login
    def iter_market_orders(self, account):
        if account.type not in (Account.TYPE_PEA, Account.TYPE_MARKET):
            return

        if account.type == Account.TYPE_PEA and account.id.startswith('CPT'):
            # Liquidity PEA have no market orders
            return

        if self.go_investments(account, get_account=True):
            # Redirection URL is https://www.linebourse.fr/ReroutageSJR
            if 'linebourse' in self.url:
                self.logger.warning('Going to Linebourse space to fetch investments.')
                # Eliminating the 3 letters prefix to match IDs on Linebourse:
                linebourse_id = account.id[3:]
                for order in self.linebourse.iter_market_orders(linebourse_id):
                    yield order

    @need_login
    def get_invest_history(self, account):
        if not self.go_investments(account):
            return
        if "linebourse" in self.url:
            for tr in self.linebourse.iter_history(re.sub('[^0-9]', '', account.id)):
                yield tr
            return

        if self.etna.is_here():
            params = self.page.params
        elif self.natixis_redirect.is_here():
            url = self.page.get_redirect()
            self.logger.debug('using redirect url %s', url)
            m = self.etna.match(url)
            if not m:
                # url can be contratPrev which is not investments
                self.logger.debug('Unable to handle this kind of contract')
                return

            params = m.groupdict()
        else:
            return

        self.natixis_history.go(**params)
        if self.natixis_unavailable_page.is_here():
            # if after this we are not redirected to the NatixisUnavaiblePage it means
            # the account is indeed available but there is no history
            self.natixis_invest.go(**params)
            if self.natixis_unavailable_page.is_here():
                raise BrowserUnavailable(self.page.get_message())
            return
        json_transactions = list(self.page.get_history())
        json_transactions.sort(reverse=True, key=lambda item: item.date)

        years = list(set(item.date.year for item in json_transactions))
        years.sort(reverse=True)

        for year in years:
            try:
                self.natixis_pdf.go(year=year, **params)
            except HTTPNotFound:
                self.logger.debug('no pdf for year %s, fallback on json transactions', year)
                for tr in json_transactions:
                    if tr.date.year == year:
                        yield tr
            except ServerError:
                return
            else:
                if self.natixis_unavailable_page.is_here():
                    # None bank statements available for this account
                    # So using json transactions
                    for tr in json_transactions:
                        if tr.date.year == year:
                            yield tr
                else:
                    history = list(self.page.get_history())
                    history.sort(reverse=True, key=lambda item: item.date)
                    for tr in history:
                        yield tr

    @need_login
    def get_profile(self):
        # The old way.
        self.location(self.absurl('/cyber/internet/StartTask.do?taskInfoOID=accueil&token=%s' % self.token, base=True))

        if not self.token or self.response.headers.get('ate-taskInfoOID', '') != 'accueil':
            # The new way.
            self.new_front_start_profile()
            self.location(
                self.absurl('/cyber/internet/StartTask.do?taskInfoOID=accueil&token=%s' % self.token, base=True)
            )

        if not self.page.is_profile_here():
            raise BrowserUnavailable()

        if not self.page.is_profile_unavailable():
            # For some users this page is not accessible.
            return self.page.get_profile()

    def new_front_start_profile(self):
        data = {
            'integrationMode': 'INTERNET_89C3',
            'realOrigin': self.BASEURL,
        }

        if not self.home_page.is_here():
            self.location('/cyber/internet/Login.do', data=data)

        data['taskId'] = self.page.get_profile_type()
        self.location('/cyber/internet/Login.do', data=data)  # It's not a real login request

    @retry(LoggedOut)
    @need_login
    def get_advisor(self):
        for taskInfoOID in ['accueil', 'contacter']:
            data = OrderedDict([('taskInfoOID', taskInfoOID), ('token', self.token)])
            self.location(self.absurl('/cyber/internet/StartTask.do', base=True), params=data)
            if taskInfoOID == "accueil":
                advisor = self.page.get_advisor()
                if not advisor:
                    break
            else:
                self.page.update_agency(advisor)
        return iter([advisor])

    @need_login
    def iter_subscriptions(self):
        # specify the website url in order to avoid 404 errors.
        # 404 errors occur when the baseurl is a website we have
        # been redirected to, like natixis or linebourse
        self.basic_token_page.go(website=self.website)
        headers = {'Authorization': 'Basic %s' % self.page.get_basic_token()}
        response = self.location('/as-bp/as/2.0/tokens', method='POST', headers=headers)
        self.documents_headers = {'Authorization': 'Bearer %s' % response.json()['access_token']}

        try:
            self.subscriber_page.go(
                headers=self.documents_headers
            )
        except ClientError as err:
            response = err.response
            if response.status_code == 400 and "ERREUR_ACCES_NOT_ALLOWED" in response.json().get('code', ''):
                # {"code":"ERREUR_ACCES_NOT_ALLOWED_MDE_MOBILE","message":"Vous n'avez pas l'accès à cette application."}
                # The user did not activate the numeric vault for dematerialized documents
                return []
            raise

        if self.page.get_status_dematerialized() == 'CGDN':
            # A status different than 1 means either the demateralization isn't enabled
            # or not available for this connection
            return []

        subscriber = self.page.get_subscriber()
        params = {'type': 'dematerialisationEffective'}
        self.location('/api-bp/wapi/2.0/abonnes/current2/contrats', params=params, headers=self.documents_headers)
        return self.page.get_subscriptions(subscriber=subscriber)

    @need_login
    def iter_documents(self, subscription):
        now = now_as_tz('Europe/Paris')
        # website says we can't get documents more than one year range, even if we can get 5 years
        # but they tell us this overload their server
        first_date = now - relativedelta(years=1)
        start_date = first_date.strftime('%Y-%m-%dT%H:%M:%S.000+00:00')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%S.000+00:00')
        body = {
            'inTypeRecherche': {'type': 'typeRechercheDocument', 'code': 'DEMAT'},
            'inDateDebut': start_date,
            'inDateFin': end_date,
            'inListeIdentifiantsContrats': [
                {'identifiantContrat': {'identifiant': subscription.id, 'codeBanque': subscription._bank_code}},
            ],
            'inListeTypesDocuments': [
                {'typeDocument': {'code': 'EXTRAIT', 'label': 'Extrait de compte', 'type': 'referenceLogiqueDocument'}},
                # space at the end of 'RELVCB ' is mandatory
                {'typeDocument': {'code': 'RELVCB ', 'label': 'Relevé Carte Bancaire', 'type': 'referenceLogiqueDocument'}},
            ],
        }
        # if the syntax is not exactly the correct one we have an error 400 for card statement
        # banquepopulaire has subdomain so the param change if we are in subdomain or not
        # if we are in subdomain the param for card statement is 'RLVCB  '
        # else the param is 'RELVCB '
        try:
            self.documents_page.go(json=body, headers=self.documents_headers)
        except ClientError as e:
            if e.response.status_code == 400:
                unavailable_page = UnavailableDocumentsPage(self, e.response)
                if unavailable_page.is_here():
                    raise BrowserUnavailable()

                body['inListeTypesDocuments'][1] = {
                    'typeDocument': {
                        # two spaces at the end of 'RLVCB  ' is mandatory
                        'code': 'RLVCB  ',
                        'label': 'Relevé Carte Bancaire',
                        'type': 'referenceLogiqueDocument',
                    },
                }
                self.documents_page.go(json=body, headers=self.documents_headers)
            else:
                raise

        return self.page.iter_documents(subid=subscription.id)

    @retry(ClientError)
    def download_document(self, document):
        return self.open(document.url, headers=self.documents_headers).content

    def get_current_subbank(self):
        match = re.search(r'icgauth.(?P<domaine>[\.a-z]*).fr', self.url)
        if match:
            self.current_subbank = match.group('domaine')
        else:
            self.current_subbank = 'banquepopulaire'

    @need_login
    def get_owner_type(self):
        if self.is_creditmaritime:
            self.new_first_login_page.go()
        else:
            self.first_login_page.go()  # the old website version

        if not self.home_page.is_here():
            # The new way.
            self.new_front_start_profile()
        if self.home_page.is_here():
            return self.page.get_owner_type()


class iter_retry(object):
    # when the callback is retried, it will create a new iterator, but we may already yielded
    # some values, so we need to keep track of them and seek in the middle of the iterator

    def __init__(self, cb, remaining=4, value=None, exc_check=Exception, logger=None):
        self.cb = cb
        self.it = value
        self.items = []
        self.remaining = remaining
        self.exc_check = exc_check
        self.logger = logger

    def __iter__(self):
        return self

    def __next__(self):
        if self.remaining <= 0:
            raise BrowserUnavailable('Site did not reply successfully after multiple tries')

        if self.it is None:
            self.it = self.cb()

            # recreated iterator, consume previous items
            try:
                nb = -1
                for sent in self.items:
                    new = next(self.it)
                    if hasattr(new, 'to_dict'):
                        equal = sent.to_dict() == new.to_dict()
                    else:
                        equal = sent == new
                    if not equal:
                        # safety is not guaranteed
                        raise BrowserUnavailable('Site replied inconsistently between retries, %r vs %r', sent, new)
            except StopIteration:
                raise BrowserUnavailable(
                    'Site replied fewer elements (%d) than last iteration (%d)', nb + 1, len(self.items)
                )
            except self.exc_check as exc:
                if self.logger:
                    self.logger.info('%s raised, retrying', exc)
                self.it = None
                self.remaining -= 1
                return next(self)

        # return one item
        try:
            obj = next(self.it)
        except self.exc_check as exc:
            if self.logger:
                self.logger.info('%s raised, retrying', exc)
            self.it = None
            self.remaining -= 1
            return next(self)
        else:
            self.items.append(obj)
            return obj

    next = __next__
