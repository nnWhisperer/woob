# -*- coding: utf-8 -*-

# Copyright(C) 2012-2013 Florent Fourcot
#
# This file is part of woob.
#
# woob is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# woob is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with woob. If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function

from decimal import Decimal
import sys

from woob.capabilities.bill import CapDocument, Detail, Subscription
from woob.capabilities.profile import CapProfile
from woob.tools.application.repl import ReplApplication, defaultcount
from woob.tools.application.formatters.iformatter import PrettyFormatter
from woob.tools.application.base import MoreResultsAvailable
from woob.tools.application.captcha import CaptchaMixin
from woob.core import CallErrors
from woob.exceptions import CaptchaQuestion
from woob.capabilities.captcha import exception_to_job


__all__ = ['AppBill']


class SubscriptionsFormatter(PrettyFormatter):
    MANDATORY_FIELDS = ('id', 'label')

    def get_title(self, obj):
        if obj.renewdate:
            return u"%s - %s" % (obj.label, obj.renewdate.strftime('%d/%m/%y'))
        return obj.label


class AppBill(CaptchaMixin, ReplApplication):
    APPNAME = 'bill'
    OLD_APPNAME = 'boobill'
    VERSION = '3.0'
    COPYRIGHT = 'Copyright(C) 2012-YEAR Florent Fourcot'
    DESCRIPTION = 'Console application allowing to get/download documents and bills.'
    SHORT_DESCRIPTION = "get/download documents and bills"
    CAPS = CapDocument
    COLLECTION_OBJECTS = (Subscription, )
    EXTRA_FORMATTERS = {'subscriptions':   SubscriptionsFormatter,
                        }
    DEFAULT_FORMATTER = 'table'
    COMMANDS_FORMATTERS = {'subscriptions':   'subscriptions',
                           'ls':              'subscriptions',
                          }

    def load_default_backends(self):
        self.load_backends(CapDocument, storage=self.create_storage())

    def main(self, argv):
        self.load_config()
        return ReplApplication.main(self, argv)

    def exec_method(self, id, method):
        l = []
        id, backend_name = self.parse_id(id)

        if not id:
            for subscrib in self.get_object_list('iter_subscription'):
                l.append((subscrib.id, subscrib.backend))
        else:
            l.append((id, backend_name))

        more_results = []
        not_implemented = []
        self.start_format()
        for id, backend in l:
            names = (backend,) if backend is not None else None
            try:
                for result in self.do(method, id, backends=names):
                    self.format(result)
            except CallErrors as errors:
                for backend, error, backtrace in errors:
                    if isinstance(error, MoreResultsAvailable):
                        more_results.append(id + u'@' + backend.name)
                    elif isinstance(error, NotImplementedError):
                        if backend not in not_implemented:
                            not_implemented.append(backend)
                    else:
                        self.bcall_error_handler(backend, error, backtrace)

        if len(more_results) > 0:
            print('Hint: There are more results available for %s (use option -n or count command)' % (', '.join(more_results)), file=self.stderr)
        for backend in not_implemented:
            print(u'Error(%s): This feature is not supported yet by this backend.' % backend.name, file=self.stderr)

    def do_subscriptions(self, line):
        """
        subscriptions

        List all subscriptions.
        """
        return self.do_ls(line)

    def do_details(self, id):
        """
        details [ID]

        Get details of subscriptions.
        If no ID given, display all details of all backends.
        """
        l = []
        id, backend_name = self.parse_id(id)

        if not id:
            for subscrib in self.get_object_list('iter_subscription'):
                l.append((subscrib.id, subscrib.backend))
        else:
            l.append((id, backend_name))

        for id, backend in l:
            names = (backend,) if backend is not None else None
            # XXX: should be generated by backend? -Flo
            # XXX: no, but you should do it in a specific formatter -romain
            # TODO: do it, and use exec_method here. Code is obsolete
            mysum = Detail()
            mysum.label = u"Sum"
            mysum.infos = u"Generated by bill"
            mysum.price = Decimal("0.")

            self.start_format()
            for detail in self.do('get_details', id, backends=names):
                self.format(detail)
                mysum.price = detail.price + mysum.price

            self.format(mysum)

    def do_balance(self, id):
        """
        balance [ID]

        Get balance of subscriptions.
        If no ID given, display balance of all backends.
        """

        self.exec_method(id, 'get_balance')

    @defaultcount(10)
    def do_history(self, id):
        """
        history [ID]

        Get the history of subscriptions.
        If no ID given, display histories of all backends.
        """
        self.exec_method(id, 'iter_bills_history')

    @defaultcount(10)
    def do_documents(self, id):
        """
        documents [ID]

        Get the list of documents for subscriptions.
        If no ID given, display documents of all backends
        """
        self.exec_method(id, 'iter_documents')

    @defaultcount(10)
    def do_bills(self, id):
        """
        bills [ID]

        Get the list of bills documents for subscriptions.
        If no ID given, display bills of all backends
        """
        self.exec_method(id, 'iter_bills')

    def do_download(self, line, force_pdf=False):
        """
        download [DOC_ID | all] [FILENAME]

        download DOC_ID [FILENAME]

        download the document
        DOC_ID is the identifier of the document (hint: try documents command)
        FILENAME is where to write the file. If FILENAME is '-',
        the file is written to stdout.

        download all [SUB_ID]

        You can use special word "all" and download all documents of
        subscription identified by SUB_ID.
        If SUB_ID is not given, download documents of all subscriptions.
        """
        id, dest = self.parse_command_args(line, 2, 1)
        id, backend_name = self.parse_id(id)
        if not id:
            print('Error: please give a document ID (hint: use documents command)', file=self.stderr)
            return 2

        if id == 'all':
            return self.download_all(dest, force_pdf)

        names = (backend_name,) if backend_name is not None else None

        document, = self.do('get_document', id, backends=names)
        if not document:
            print('Error: document not found')
            return 1

        if dest is None:
            dest = id + "." + (document.format if not force_pdf else 'pdf')

        for buf in self.do('download_document' if not force_pdf else 'download_document_pdf', document, backends=names):
            if buf:
                if dest == "-":
                    if sys.version_info.major >= 3:
                        self.stdout.buffer.write(buf)
                    else:
                        self.stdout.stream.write(buf)
                else:
                    try:
                        with open(dest, 'wb') as f:
                            f.write(buf)
                    except IOError as e:
                        print('Unable to write document in "%s": %s' % (dest, e), file=self.stderr)
                        return 1
                return

    def do_download_pdf(self, line):
        """
        download_pdf [id | all]

        download function with forced PDF conversion.
        """

        return self.do_download(line, force_pdf=True)

    def download_all(self, sub_id, force_pdf):
        if sub_id:
            sub_id, backend_name = self.parse_id(sub_id)
            names = (backend_name,) if backend_name else None
            subscription, = self.do('get_subscription', sub_id, backends=names)
            if not self.download_subscription(subscription, force_pdf):
                return 1
        else:
            for subscription in self.do('iter_subscription'):
                if not self.download_subscription(subscription, force_pdf):
                    return 1

    def download_subscription(self, subscription, force_pdf):
        for document in self.do('iter_documents', subscription, backends=(subscription.backend,)):
            if not self.download_doc(document, force_pdf):
                return False
        return True

    def download_doc(self, document, force_pdf):
        if force_pdf:
            method = 'download_document_pdf'
        else:
            method = 'download_document'

        dest = document.id + "." + (document.format if not force_pdf else 'pdf')

        for buf in self.do(method, document, backends=(document.backend,)):
            if buf:
                try:
                    with open(dest, 'wb') as f:
                        f.write(buf)
                except IOError as e:
                    print('Unable to write bill in "%s": %s' % (dest, e), file=self.stderr)
                    return False
        return True

    def do_profile(self, line):
        """
        profile

        Display detailed information about person or company.
        """
        self.start_format()
        for profile in self.do('get_profile', caps=CapProfile):
            self.format(profile)

    def bcall_error_handler(self, backend, error, backtrace):
        """
        Handler for an exception inside the CallErrors exception.

        This method can be overridden to support more exceptions types.
        """
        if isinstance(error, CaptchaQuestion):
            if not self.captcha_woob.count_backends():
                print('Error(%s): Site requires solving a CAPTCHA but no CapCaptchaSolver backends were configured' % backend.name,
                      file=self.stderr)
                return False

            print('Info(%s): Encountered CAPTCHA, please wait for its resolution, it can take dozens of seconds' % backend.name, file=self.stderr)
            job = exception_to_job(error)
            self.solve_captcha(job, backend)
            return False

        return super(ReplApplication, self).bcall_error_handler(backend, error, backtrace)
