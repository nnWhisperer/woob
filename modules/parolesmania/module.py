# -*- coding: utf-8 -*-

# Copyright(C) 2013 Julien Veyssier
#
# This file is part of a woob module.
#
# This woob module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This woob module is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this woob module. If not, see <http://www.gnu.org/licenses/>.

from woob.capabilities.lyrics import CapLyrics, SongLyrics
from woob.tools.backend import Module
from woob.tools.compat import quote_plus

from .browser import ParolesmaniaBrowser


__all__ = ['ParolesmaniaModule']


class ParolesmaniaModule(Module, CapLyrics):
    NAME = 'parolesmania'
    MAINTAINER = u'Julien Veyssier'
    EMAIL = 'eneiluj@gmx.fr'
    VERSION = '3.0'
    DESCRIPTION = 'Paroles Mania lyrics website'
    LICENSE = 'AGPLv3+'
    BROWSER = ParolesmaniaBrowser

    def get_lyrics(self, id):
        return self.browser.get_lyrics(id)

    def iter_lyrics(self, criteria, pattern):
        return self.browser.iter_lyrics(criteria, quote_plus(pattern.encode('utf-8')))

    def fill_songlyrics(self, songlyrics, fields):
        if 'content' in fields:
            sl = self.get_lyrics(songlyrics.id)
            songlyrics.content = sl.content
        return songlyrics

    OBJECTS = {
        SongLyrics: fill_songlyrics
    }
