# -*- coding: utf-8 -*-

# Copyright(C) 2010-2011 Romain Bignon
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

from __future__ import with_statement

from copy import copy
from httplib import BadStatusLine
from logging import warning
import mechanize
import os
import sys
import re
import tempfile
from threading import RLock
import time
import urllib
import urllib2
import mimetypes

from weboob.tools.decorators import retry
from weboob.tools.log import getLogger
from weboob.tools.mech import ClientForm
ControlNotFoundError = ClientForm.ControlNotFoundError
from weboob.tools.parsers import get_parser

# Try to load cookies
try:
    from .firefox_cookies import FirefoxCookieJar
except ImportError, e:
    warning("Unable to store cookies: %s" % e)
    HAVE_COOKIES = False
else:
    HAVE_COOKIES = True


__all__ = ['BrowserIncorrectPassword', 'BrowserBanned', 'BrowserUnavailable', 'BrowserRetry',
           'BrowserHTTPNotFound', 'BrowserHTTPError', 'BasePage', 'BaseBrowser', 'StandardBrowser']


# Exceptions
class BrowserIncorrectPassword(Exception):
    pass


class BrowserBanned(BrowserIncorrectPassword):
    pass


class BrowserUnavailable(Exception):
    pass

class BrowserHTTPNotFound(BrowserUnavailable):
    pass

class BrowserHTTPError(BrowserUnavailable):
    pass


class BrowserRetry(Exception):
    pass


class NoHistory(object):
    """
    We don't want to fill memory with history
    """
    def __init__(self):
        pass

    def add(self, request, response):
        pass

    def back(self, n, _response):
        pass

    def clear(self):
        pass

    def close(self):
        pass

class BrokenPageError(Exception):
    pass

class BasePage(object):
    """
    Base page
    """
    def __init__(self, browser, document, url='', groups=None, group_dict=None, logger=None):
        self.browser = browser
        self.parser = browser.parser
        self.document = document
        self.url = url
        self.groups = groups
        self.group_dict = group_dict
        self.logger = getLogger('page', logger)

    def on_loaded(self):
        """
        Called when the page is loaded.
        """
        pass

def check_location(func):
    def inner(self, *args, **kwargs):
        if args and isinstance(args[0], basestring):
            url = args[0]
            if url.startswith('/') and (not self.request or self.request.host != self.DOMAIN):
                url = '%s://%s%s' % (self.PROTOCOL, self.DOMAIN, url)
            url = re.sub('(.*)#.*', r'\1', url)

            args = (url,) + args[1:]
        return func(self, *args, **kwargs)
    return inner

class StandardBrowser(mechanize.Browser):

    # ------ Class attributes --------------------------------------

    ENCODING = 'utf-8'
    USER_AGENTS = {
        'desktop_firefox': 'Mozilla/5.0 (X11; U; Linux x86_64; fr; rv:1.9.2.13) Gecko/20101209 Fedora/3.6.13-1.fc13 Firefox/3.6.13',
        'android': 'Mozilla/5.0 (Linux; U; Android 2.1; en-us; Nexus One Build/ERD62) AppleWebKit/530.17 (KHTML, like Gecko) Version/4.0 Mobile Safari/530.17',
        'microb': 'Mozilla/5.0 (X11; U; Linux armv7l; fr-FR; rv:1.9.2.3pre) Gecko/20100723 Firefox/3.5 Maemo Browser 1.7.4.8 RX-51 N900',
        'wget': 'Wget/1.11.4',
    }
    USER_AGENT = USER_AGENTS['desktop_firefox']
    SAVE_RESPONSES = False
    DEBUG_HTTP = False
    DEBUG_MECHANIZE = False

    responses_dirname = None
    responses_count = 0

    # ------ Browser methods ---------------------------------------

    # I'm not a robot, so disable the check of permissions in robots.txt.
    default_features = copy(mechanize.Browser.default_features)
    default_features.remove('_robots')
    default_features.remove('_refresh')

    def __init__(self, firefox_cookies=None, parser=None, history=NoHistory(), proxy=None, logger=None,
                       factory=None):
        """
        Constructor of Browser.

        @param filefox_cookies [str] Path to cookies' sqlite file.
        @param parser [IParser]  parser to use on HTML files.
        @param history [object]  History manager. Default value is an object
                                 which does not keep history.
        @param proxy [str]  proxy URL to use.
        @param factory [object] Mechanize factory. None to use Mechanize's default.
        """
        mechanize.Browser.__init__(self, history=history, factory=factory)
        self.logger = getLogger('browser', logger)

        self.addheaders = [
                ['User-agent', self.USER_AGENT]
            ]

        # Use a proxy
        self.proxy = proxy
        if proxy:
            proto = 'http'
            if proxy.find('://') >= 0:
                proto, domain = proxy.split('://', 1)
            else:
                domain = proxy
            self.set_proxies({proto: domain})

        # Share cookies with firefox
        if firefox_cookies and HAVE_COOKIES:
            self._cookie = FirefoxCookieJar(self.DOMAIN, firefox_cookies)
            self._cookie.load()
            self.set_cookiejar(self._cookie)
        else:
            self._cookie = None

        if parser is None:
            parser = get_parser()()
        elif isinstance(parser, (tuple,list,str,unicode)):
            parser = get_parser(parser)()
        self.parser = parser
        self.lock = RLock()

        if self.DEBUG_HTTP:
            # display messages from httplib
            self.set_debug_http(True)

        if self.DEBUG_MECHANIZE:
            # Enable log messages from mechanize.Browser
            self.set_debug_redirects(True)

    def __enter__(self):
        self.lock.acquire()

    def __exit__(self, t, v, tb):
        self.lock.release()

    @check_location
    @retry(BrowserHTTPError, tries=3)
    def openurl(self, *args, **kwargs):
        """
        Open an URL but do not create a Page object.
        """
        if_fail = kwargs.pop('if_fail', 'raise')
        self.logger.debug('Opening URL "%s", %s' % (args, kwargs))

        try:
            return mechanize.Browser.open_novisit(self, *args, **kwargs)
        except (mechanize.response_seek_wrapper, urllib2.HTTPError, urllib2.URLError, BadStatusLine), e:
            if if_fail == 'raise':
                raise self.get_exception(e)('%s (url="%s")' % (e, args and args[0] or 'None'))
            else:
                return None
        except (mechanize.BrowserStateError, BrowserRetry):
            if hasattr(self, 'home'):
                self.home()
            return mechanize.Browser.open(self, *args, **kwargs)

    def get_exception(self, e):
        if isinstance(e, urllib2.HTTPError) and hasattr(e, 'getcode') and e.getcode() == 404:
            return BrowserHTTPNotFound
        else:
            return BrowserHTTPError

    def readurl(self, url, *args, **kwargs):
        """
        Download URL data specifying what to do on failure (nothing by default).
        """
        if not 'if_fail' in kwargs:
            kwargs['if_fail'] = None
        result = self.openurl(url, *args, **kwargs)

        if result:
            if self.SAVE_RESPONSES:
                self.save_response(result)
            return result.read()
        else:
            return None

    def save_response(self, result, warning=False):
        """
        Save a stream to a temporary file, and log its name.
        The stream is rewinded after saving.
        """
        if self.responses_dirname is None:
            self.responses_dirname = tempfile.mkdtemp(prefix='weboob_session_')
            print >>sys.stderr, 'Debug data will be saved in this directory: %s' % self.responses_dirname
        # get the content-type, remove optionnal charset part
        mimetype = result.info().get('Content-Type', '').split(';')[0]
        # due to http://bugs.python.org/issue1043134
        if mimetype == 'text/plain':
            ext = '.txt'
        else:
            # try to get an extension (and avoid adding 'None')
            ext = mimetypes.guess_extension(mimetype, False) or ''
        response_filepath = os.path.join(self.responses_dirname, unicode(self.responses_count)+ext)
        with open(response_filepath, 'w') as f:
            f.write(result.read())
        result.seek(0)
        match_filepath = os.path.join(self.responses_dirname, 'url_response_match.txt')
        with open(match_filepath, 'a') as f:
            f.write('%s\t%s\n' % (result.geturl(), os.path.basename(response_filepath)))
        self.responses_count += 1

        msg = u'Response saved to %s' % response_filepath
        if warning:
            self.logger.warning(msg)
        else:
            self.logger.info(msg)

    def get_document(self, result):
        return self.parser.parse(result, self.ENCODING)

    def location(self, *args, **kwargs):
        return self.get_document(self.openurl(*args, **kwargs))

    @staticmethod
    def buildurl(base, *args, **kwargs):
        """
        Build an URL and escape arguments.
        You can give a serie of tuples in *args (and the order is keept), or
        a dict in **kwargs (but the order is lost).

        Example:
        >>> buildurl('/blah.php', ('a', '&'), ('b', '=')
        '/blah.php?a=%26&b=%3D'
        >>> buildurl('/blah.php', a='&', 'b'='=')
        '/blah.php?b=%3D&a=%26'

        """

        if not args:
            args = kwargs
        if not args:
            return base
        else:
            return '%s?%s' % (base, urllib.urlencode(args))

    def str(self, s):
        if isinstance(s, unicode):
            s = s.encode('iso-8859-15', 'replace')
        return s

    def set_field(self, args, label, field=None, value=None, is_list=False):
        """
        Set a value to a form field.

        @param args [dict]  arguments where to look for value.
        @param label [str]  label in args.
        @param field [str]  field name. If None, use label instead.
        @param value [str]  value to give on field.
        @param is_list [bool]  the field is a list.
        """
        try:
            if not field:
                field = label
            if args.get(label, None) is not None:
                if not value:
                    if is_list:
                        if isinstance(is_list, (list, tuple)):
                            try:
                                value = [self.str(is_list.index(args[label]))]
                            except ValueError, e:
                                if args[label]:
                                    print >>sys.stderr, '[%s] %s: %s' % (label, args[label], e)
                                return
                        else:
                            value = [self.str(args[label])]
                    else:
                        value = self.str(args[label])
                self[field] = value
        except ControlNotFoundError:
            return

class BaseBrowser(StandardBrowser):
    """
    Base browser class to navigate on a website.
    """

    # ------ Class attributes --------------------------------------

    DOMAIN = None
    PROTOCOL = 'http'
    PAGES = {}

    # ------ Abstract methods --------------------------------------

    def home(self):
        """
        Go to the home page.
        """
        if self.DOMAIN is not None:
            self.location('%s://%s/' % (self.PROTOCOL, self.DOMAIN))

    def login(self):
        """
        Login to the website.

        This function is called when is_logged() returns False and the password
        attribute is not None.
        """
        raise NotImplementedError()

    def is_logged(self):
        """
        Return True if we are logged on website. When Browser tries to access
        to a page, if this method returns False, it calls login().

        It is never called if the password attribute is None.
        """
        raise NotImplementedError()

    # ------ Browser methods ---------------------------------------

    def __init__(self, username=None, password=None, firefox_cookies=None,
                 parser=None, history=NoHistory(), proxy=None, logger=None,
                 factory=None, get_home=True):
        """
        Constructor of Browser.

        @param username [str] username on website.
        @param password [str] password on website. If it is None, Browser will
                              not try to login.
        @param filefox_cookies [str] Path to cookies' sqlite file.
        @param parser [IParser]  parser to use on HTML files.
        @param hisory [object]  History manager. Default value is an object
                                which does not keep history.
        @param proxy [str]  proxy URL to use.
        @param factory [object] Mechanize factory. None to use Mechanize's default.
        @param get_home [bool] Try to get the homepage.
        """
        StandardBrowser.__init__(self, firefox_cookies, parser, history, proxy, logger, factory)
        self.page = None
        self.last_update = 0.0
        self.username = username
        self.password = password

        if self.password and get_home:
            try:
                self.home()
            # Do not abort the build of browser when the website is down.
            except BrowserUnavailable:
                pass

    def pageaccess(func):
        """
        Decorator to use around a method which access to a page.
        """
        def inner(self, *args, **kwargs):
            if not self.page or self.password and not self.page.is_logged():
                self.home()

            return func(self, *args, **kwargs)
        return inner

    @pageaccess
    def keepalive(self):
        self.home()

    def submit(self, *args, **kwargs):
        """
        Submit the selected form.
        """
        nologin = kwargs.pop('nologin', False)
        try:
            self._change_location(mechanize.Browser.submit(self, *args, **kwargs), no_login=nologin)
        except (mechanize.response_seek_wrapper, urllib2.HTTPError, urllib2.URLError, BadStatusLine), e:
            self.page = None
            raise self.get_exception(e)(e)
        except (mechanize.BrowserStateError, BrowserRetry), e:
            raise BrowserUnavailable(e)

    def is_on_page(self, pageCls):
        return isinstance(self.page, pageCls)

    def absurl(self, rel):
        if rel is None:
            return None
        if not rel.startswith('/'):
            rel = '/' + rel
        return '%s://%s%s' % (self.PROTOCOL, self.DOMAIN, rel)

    def follow_link(self, *args, **kwargs):
        try:
            self._change_location(mechanize.Browser.follow_link(self, *args, **kwargs))
        except (mechanize.response_seek_wrapper, urllib2.HTTPError, urllib2.URLError, BadStatusLine), e:
            self.page = None
            raise self.get_exception(e)('%s (url="%s")' % (e, args and args[0] or 'None'))
        except (mechanize.BrowserStateError, BrowserRetry), e:
            self.home()
            raise BrowserUnavailable(e)

    @check_location
    @retry(BrowserHTTPError, tries=3)
    def location(self, *args, **kwargs):
        """
        Change location of browser on an URL.

        When the page is loaded, it looks up PAGES to find a regexp which
        matches, and create the object. Then, the 'on_loaded' method of
        this object is called.

        If a password is set, and is_logged() returns False, it tries to login
        with login() and reload the page.
        """
        keep_args = copy(args)
        keep_kwargs = kwargs.copy()

        no_login = kwargs.pop('no_login', False)

        try:
            self._change_location(mechanize.Browser.open(self, *args, **kwargs), no_login=no_login)
        except BrowserRetry:
            if not self.page or not args or self.page.url != args[0]:
                keep_kwargs['no_login'] = True
                self.location(*keep_args, **keep_kwargs)
        except (mechanize.response_seek_wrapper, urllib2.HTTPError, urllib2.URLError, BadStatusLine), e:
            self.page = None
            raise self.get_exception(e)('%s (url="%s")' % (e, args and args[0] or 'None'))
        except mechanize.BrowserStateError:
            self.home()
            self.location(*keep_args, **keep_kwargs)

    # DO NOT ENABLE THIS FUCKING PEACE OF CODE EVEN IF IT WOULD BE BETTER
    # TO SANITARIZE FUCKING HTML.
    #def _set_response(self, response, *args, **kwargs):
    #    import time
    #    if response and hasattr(response, 'set_data'):
    #        print time.time()
    #        r = response.read()
    #        start = 0
    #        end = 0
    #        new = ''
    #        lowr = r.lower()
    #        start = lowr[end:].find('<script')
    #        while start >= end:
    #            start_stop = start + lowr[start:].find('>') + 1
    #            new += r[end:start_stop]
    #            end = start + lowr[start:].find('</script>')
    #            new += r[start_stop:end].replace('<', '&lt;').replace('>', '&gt;')
    #            start = end + lowr[end:].find('<script')
    #        new += r[end:]
    #        response.set_data(new)
    #        print time.time()
    #    mechanize.Browser._set_response(self, response, *args, **kwargs)

    def _change_location(self, result, no_login=False):
        """
        This function is called when we have moved to a page, to load a Page
        object.
        """

        # Find page from url
        pageCls = None
        page_groups = None
        page_group_dict = None
        for key, value in self.PAGES.items():
            regexp = re.compile('^%s$' % key)
            m = regexp.match(result.geturl())
            if m:
                pageCls = value
                page_groups = m.groups()
                page_group_dict = m.groupdict()
                break

        # Not found
        if not pageCls:
            self.page = None
            self.logger.warning('There isn\'t any page corresponding to URL %s' % result.geturl())
            self.save_response(result, warning=True)
            return

        self.logger.debug('[user_id=%s] Went on %s' % (self.username, result.geturl()))
        self.last_update = time.time()

        if self.SAVE_RESPONSES:
            self.save_response(result)

        document = self.get_document(result)
        self.page = pageCls(self, document, result.geturl(), groups=page_groups, group_dict=page_group_dict, logger=self.logger)

        if not no_login and self.password is not None and not self.is_logged():
            self.logger.debug('!! Relogin !!')
            self.login()
            raise BrowserRetry()

        self.page.on_loaded()

        if self._cookie:
            self._cookie.save()
