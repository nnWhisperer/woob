# -*- coding: utf-8 -*-

# Copyright(C) 2014 Romain Bignon
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

from __future__ import absolute_import

from dateutil.parser import parse as parse_date
import datetime
from decimal import Decimal, InvalidOperation
import re
from weboob.capabilities.base import empty

_NO_DEFAULT = object()


class _Filter(object):
    _creation_counter = 0

    def __init__(self):
        self._creation_counter = _Filter._creation_counter
        _Filter._creation_counter += 1


class Filter(_Filter):
    """
    Class used to filter on a HTML element given as call parameter to return
    matching elements.

    Filters can be chained, so the parameter supplied to constructor can be
    either a xpath selector string, or an other filter called before.

    >>> from lxml.html import etree
    >>> f = CleanDecimal(CleanText('//p'))
    >>> f(etree.fromstring('<html><body><p>blah: <span>229,90</span></p></body></html>'))
    Decimal('229.90')
    """

    def __init__(self, selector=None):
        super(Filter, self).__init__()
        self.selector = selector

    @classmethod
    def select(cls, selector, item):
        if isinstance(selector, basestring):
            return item.xpath(selector)
        elif callable(selector):
            return selector(item)
        else:
            return selector

    def __call__(self, item):
        return self.filter(self.select(self.selector, item))

    def filter(self, value):
        """
        This method have to be overrided by children classes.
        """
        raise NotImplementedError()


class Env(_Filter):
    """
    Filter to get environment value of the item.

    It is used for example to get page parameters, or when there is a parse()
    method on ItemElement.
    """
    def __init__(self, name):
        super(Env, self).__init__()
        self.name = name

    def __call__(self, item):
        return item.env[self.name]


class TableCell(_Filter):
    """
    Used with TableElement, it get the cell value from its name.

    For example:

        class table(TableElement):
            head_xpath = '//table/thead/th'
            item_xpath = '//table/tbody/tr'

            col_date =    u'Date'
            col_label =   [u'Name', u'Label']

            class item(ItemElement):
                klass = Object
                obj_date = Date(TableCell('date'))
                obj_label = CleanText(TableCell('label'))
    """

    def __init__(self, *names, **kwargs):
        super(TableCell, self).__init__()
        self.names = names
        self.default = kwargs.pop('default', _NO_DEFAULT)

    def __call__(self, item):
        for name in self.names:
            idx = item.parent.get_colnum(name)
            if idx is not None:
                return item.xpath('./td[%s]' % (idx + 1))

        if self.default is not _NO_DEFAULT:
            return self.default
        raise KeyError('Unable to find column %s' % ' or '.join(self.names))


class CleanText(Filter):
    """
    Get a cleaned text from an element.

    It first replaces all tabs and multiple spaces to one space and strip the result
    string.
    Second, it replaces all symbols given in second argument.
    """
    def __init__(self, selector, symbols=''):
        super(CleanText, self).__init__(selector)
        self.symbols = symbols

    def filter(self, txt):
        if isinstance(txt, (tuple,list)):
            txt = ' '.join(map(self.clean, txt))

        txt = self.clean(txt)
        return self.remove(txt, self.symbols)

    @classmethod
    def clean(cls, txt):
        if not isinstance(txt, basestring):
            txt = [t.strip() for t in txt.itertext()]
            txt = u' '.join(txt)                 # 'foo   bar'
        txt = re.sub(u'[\\s\xa0\t]+', u' ', txt)   # 'foo bar'
        return txt.strip()

    @classmethod
    def remove(cls, txt, symbols):
        for symbol in symbols:
            txt = txt.replace(symbol, '')
        return txt


class CleanDecimal(CleanText):
    """
    Get a cleaned Decimal value from an element.
    """
    def __init__(self, selector, replace_dots=True, default=_NO_DEFAULT):
        super(CleanDecimal, self).__init__(selector)
        self.replace_dots = replace_dots
        self.default = default

    def filter(self, text):
        text = super(CleanDecimal, self).filter(text)
        if self.replace_dots:
            text = text.replace('.','').replace(',','.')
        try:
            return Decimal(re.sub(ur'[^\d\-\.]', '', text))
        except InvalidOperation as e:
            if self.default is not _NO_DEFAULT:
                return Decimal(self.default)
            else:
                raise InvalidOperation(e)


class Attr(Filter):
    def __init__(self, selector, attr, default=_NO_DEFAULT):
        super(Attr, self).__init__(selector)
        self.attr = attr
        self.default = default

    def filter(self, el):
        try:
            return el[0].attrib[self.attr]
        except IndexError:
            if self.default is not _NO_DEFAULT:
                return self.default
            raise ValueError('Unable to find link %s' % self.selector)
        except KeyError:
            if self.default is not _NO_DEFAULT:
                return self.default
            else:
                raise KeyError('Link %s does not has attribute %s' % (el[0], self.attr))


class Link(Attr):
    """
    Get the link uri of an element.

    If the <a> tag is not found, an exception IndexError is raised.
    """
    def __init__(self, selector, default=_NO_DEFAULT):
        super(Link, self).__init__(selector, 'href', default)


class Field(_Filter):
    """
    Get the attribute of object.
    """
    def __init__(self, name):
        super(Field, self).__init__()
        self.name = name

    def __call__(self, item):
        return item.use_selector(getattr(item, 'obj_%s' % self.name))


class Regexp(Filter):
    r"""
    Apply a regex.

    >>> from lxml.html import etree
    >>> f = Regexp(CleanText('//p'), r'Date: (\d+)/(\d+)/(\d+)', '\\3-\\2-\\1')
    >>> f(etree.fromstring('<html><body><p>Date: <span>13/08/1988</span></p></body></html>'))
    u'1988-08-13'
    """
    def __init__(self, selector, pattern, template=None, flags=0, default=_NO_DEFAULT):
        super(Regexp, self).__init__(selector)
        self.pattern = pattern
        self.regex = re.compile(pattern, flags)
        self.template = template
        self.default = default

    def filter(self, txt):
        if isinstance(txt, (tuple,list)):
            txt = ' '.join([t.strip() for t in txt.itertext()])

        mobj = self.regex.search(txt)
        if not mobj:
            if self.default is not _NO_DEFAULT:
                return self.default
            else:
                raise KeyError('Unable to match %s in %r' % (self.pattern, txt))

        if self.template is None:
            return next(g for g in mobj.groups() if g is not None)
        else:
            return mobj.expand(self.template)


class Map(Filter):
    def __init__(self, selector, map_dict, default=_NO_DEFAULT):
        super(Map, self).__init__(selector)
        self.map_dict = map_dict
        self.default = default

    def filter(self, txt):
        try:
            return self.map_dict[txt]
        except KeyError:
            if self.default is not _NO_DEFAULT:
                return self.default
            else:
                raise KeyError('Unable to handle %r' % txt)


class DateTime(Filter):
    def filter(self, txt):
        if empty(txt):
            return txt
        return parse_date(txt)


class Date(DateTime):
    def filter(self, txt):
        datetime = super(Date, self).filter(txt)
        if datetime is not None:
            return datetime.date


class Time(Filter):
    klass = datetime.time
    regexp = re.compile(ur'(?P<hh>\d+):?(?P<mm>\d+)(:(?P<ss>\d+))?')
    kwargs = {'hour': 'hh', 'minute': 'mm', 'second': 'ss'}

    def __init__(self, selector, default=_NO_DEFAULT):
        super(Time, self).__init__(selector)
        self.default = default

    def filter(self, txt):
        m = self.regexp.search(txt)
        if m:
            kwargs = {}
            for key, index in self.kwargs.iteritems():
                kwargs[key] = int(m.groupdict()[index] or 0)
            return self.klass(**kwargs)

        if self.default is not _NO_DEFAULT:
            return self.default
        else:
            raise ValueError('Unable to find time in %r' % txt)


class Duration(Time):
    klass = datetime.timedelta
    regexp = re.compile(ur'((?P<hh>\d+)[:;])?(?P<mm>\d+)[;:](?P<ss>\d+)')
    kwargs = {'hours': 'hh', 'minutes': 'mm', 'seconds': 'ss'}


class MultiFilter(Filter):
    def __init__(self, *args):
        super(MultiFilter, self).__init__(args)

    def __call__(self, item):
        values = [self.select(selector, item) for selector in self.selector]
        return self.filter(tuple(values))

    def filter(self, values):
        raise NotImplementedError()


class Format(MultiFilter):
    def __init__(self, fmt, *args):
        super(Format, self).__init__(*args)
        self.fmt = fmt

    def filter(self, values):
        return self.fmt % values


class Join(Filter):
    def __init__(self, pattern, selector):
        super(Join, self).__init__(selector)
        self.pattern = pattern

    def filter(self, el):
        res = u''
        for li in el:
            res += self.pattern % CleanText.clean(li)

        return res
