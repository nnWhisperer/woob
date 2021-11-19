# -*- coding: utf-8 -*-

# Copyright(C) 2019      Budget Insight
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

from __future__ import unicode_literals

import re
from hashlib import sha1

from woob.browser.elements import method, ListElement, ItemElement, DictElement
from woob.browser.filters.html import Link
from woob.browser.filters.standard import CleanText, Coalesce, Regexp, CleanDecimal, Currency, Field, Env, Format
from woob.browser.filters.json import Dict
from woob.browser.pages import LoggedPage, HTMLPage, PartialHTMLPage, RawPage, JsonPage
from woob.capabilities.bill import Subscription, Bill, Document, DocumentTypes
from woob.exceptions import BrowserUnavailable
from woob.tools.compat import html_unescape
from woob.tools.date import parse_french_date
from woob.tools.json import json


class LoginPage(HTMLPage):
    def is_here(self):
        return self.doc.xpath('//form[contains(@id, "CompteForm")]')

    def login(self, username, password, _ct):
        form = self.get_form(id='connexioncompte_2connexionCompteForm')
        form['connexioncompte_2numSecuriteSociale'] = username
        form['connexioncompte_2codeConfidentiel'] = password
        form['_ct'] = _ct
        form.submit()

    def get_error_message(self):
        return Coalesce(
            CleanText('//div[@id="loginPage"]//div[has-class("zone-alerte") and not(has-class("hidden"))]/span'),
            CleanText('//div[@class="centrepage compte_bloque"]//p[@class="msg_erreur"]'),
            default=None
        )(self.doc)


class NewPasswordPage(HTMLPage):
    pass


class CtPage(RawPage):
    # the page contains only _ct value
    def get_ct_value(self):
        return re.search(r'_ct:(.*)', self.text).group(1)


class RedirectPage(LoggedPage, HTMLPage):
    REFRESH_MAX = 0
    REFRESH_XPATH = '//meta[@http-equiv="refresh"]'


class CguPage(LoggedPage, HTMLPage):
    def get_cgu_message(self):
        return CleanText('//div[@class="page_nouvelles_cgus"]/p')(self.doc)


class ErrorPage(HTMLPage):
    def on_load(self):
        # message is: "Oups... votre compte ameli est momentanément indisponible. Il sera de retour en pleine forme très bientôt."
        # nothing we can do, but retry later
        raise BrowserUnavailable(html_unescape(CleanText('//div[@class="mobile"]/p')(self.doc)))


class SubscriptionPage(LoggedPage, HTMLPage):
    def get_subscription(self):
        sub = Subscription()
        # DON'T TAKE social security number for id because it's a very confidential data, take birth date instead
        sub.id = CleanText('//button[@id="idAssure"]//td[@class="dateNaissance"]')(self.doc).replace('/', '')
        sub.label = sub.subscriber = CleanText('//div[@id="pageAssure"]//span[@class="NomEtPrenomLabel"]')(self.doc)

        return sub


class DocumentsDetailsPage(LoggedPage, PartialHTMLPage):
    ENCODING = 'utf-8'

    def build_doc(self, content):
        res = json.loads(content)
        return super(DocumentsDetailsPage, self).build_doc(res['tableauPaiement'].encode('utf-8'))

    @method
    class iter_documents(ListElement):
        item_xpath = '//ul[@id="unordered_list"]//li[has-class("rowitem")]'

        class item(ItemElement):
            klass = Bill

            def obj_id(self):
                _id = Regexp(Field('url'), r'idPaiement=(.*)')(self)
                # idPaiement is very long, about 192 char, and sometimes they change it, (even existing id)
                # to make it much longer, (because 120 char wasn't enough apparently)
                return '%s_%s' % (Env('subid')(self), sha1(_id.encode('utf-8')).hexdigest())

            obj_label = CleanText('.//div[has-class("col-label")]')
            obj_price = CleanDecimal.French('.//div[has-class("col-montant")]/span')
            obj_currency = Currency('.//div[has-class("col-montant")]/span')
            obj_url = Link('.//div[@class="col-download"]/a')
            obj_format = 'pdf'

            def obj_date(self):
                year = Regexp(
                    CleanText('./preceding-sibling::li[@class="rowdate"]//span[@class="mois"]'), r'(\d+)'
                )(self)
                day_month = CleanText('.//div[has-class("col-date")]/span')(self)

                return parse_french_date(day_month + ' ' + year)


class DocumentsFirstSummaryPage(LoggedPage, HTMLPage):

    @method
    class iter_documents(ListElement):
        item_xpath = '//ul[@id="unordered_list"]//li[@class="rowdate" and .//span[@class="blocTelecharger"]]'

        class item(ItemElement):
            klass = Document

            obj_type = DocumentTypes.BILL
            obj_label = Format('%s %s', CleanText('.//span[@class="libelle"]'), CleanText('.//span[@class="mois"]'))
            obj_url = Link('.//div[@class="col-telechargement"]//a')
            obj_format = 'pdf'

            def obj_date(self):
                year = Regexp(CleanText('.//span[@class="mois"]'), r'(\d+)')(self)
                month = Regexp(CleanText('.//span[@class="mois"]'), r'(\D+)')(self)

                return parse_french_date(month + ' ' + year)

            def obj_id(self):
                year = Regexp(CleanText('.//span[@class="mois"]'), r'(\d+)')(self)
                month = Regexp(CleanText('.//span[@class="mois"]'), r'(\D+)')(self)

                return '%s_%s' % (Env('subid')(self), parse_french_date(month + ' ' + year).strftime('%Y%m'))


class DocumentsLastSummaryPage(LoggedPage, JsonPage):

    @method
    class iter_documents(DictElement):

        def find_elements(self):
            for doc in self.el['listeDecomptes']:
                if doc['montant']:
                    yield doc

        class item(ItemElement):
            klass = Document

            obj_type = DocumentTypes.BILL
            obj_url = Dict('urlPDF')
            obj_format = 'pdf'
            obj_label = Format('Relevé mensuel %s', CleanText(Dict('mois')))

            def obj_date(self):
                year = Regexp(CleanText(Dict('mois')), r'(\d+)')(self)
                month = Regexp(CleanText(Dict('mois')), r'(\D+)')(self)

                return parse_french_date(month + ' ' + year)

            def obj_id(self):
                year = Regexp(CleanText(Dict('mois')), r'(\d+)')(self)
                month = Regexp(CleanText(Dict('mois')), r'(\D+)')(self)

                return '%s_%s' % (Env('subid')(self), parse_french_date(month + ' ' + year).strftime('%Y%m'))
