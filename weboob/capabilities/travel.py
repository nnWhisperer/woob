# -*- coding: utf-8 -*-

# Copyright(C) 2010-2011 Romain Bignon, Julien Hebert
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


from datetime import time, datetime, timedelta

from .base import IBaseCap, CapBaseObject


__all__ = ['Departure', 'ICapTravel', 'Station']


class Station(CapBaseObject):
    def __init__(self, id, name):
        CapBaseObject.__init__(self, id)
        self.add_field('name', basestring, name)

    def __repr__(self):
        return "<Station id=%r name=%r>" % (self.id, self.name)

class Departure(CapBaseObject):
    def __init__(self, id, _type, _time):
        CapBaseObject.__init__(self, id)

        self.add_field('type', basestring, _type)
        self.add_field('time', datetime, _time)
        self.add_field('departure_station', basestring)
        self.add_field('arrival_station', basestring)
        self.add_field('late', time, time())
        self.add_field('information', basestring)
        self.add_field('plateform', basestring)

    def __repr__(self):
        return u"<Departure id=%r type=%r time=%r departure=%r arrival=%r>" % (
            self.id, self.type, self.time.strftime('%H:%M'), self.departure_station, self.arrival_station)

class RoadStep(CapBaseObject):
    def __init__(self, id):
        CapBaseObject.__init__(self, id)

        self.add_field('line', basestring)
        self.add_field('start_time', time)
        self.add_field('end_time', time)
        self.add_field('departure', unicode)
        self.add_field('arrival', unicode)
        self.add_field('duration', timedelta)

class RoadmapError(Exception):
    pass

class RoadmapFilters(CapBaseObject):
    def __init__(self):
        CapBaseObject.__init__(self, '')

        self.add_field('departure_time', datetime)
        self.add_field('arrival_time', datetime)

class ICapTravel(IBaseCap):
    def iter_station_search(self, pattern):
        """
        Iterates on search results of stations.

        @param pattern [str]  the search pattern
        @return [iter]  the of Station objects
        """
        raise NotImplementedError()

    def iter_station_departures(self, station_id, arrival_id):
        """
        Iterate on departures.

        @param station_id [id]  the station id
        @param arrival_id [id]  optionnal arrival station id
        @return [iter]  result of Departure objects
        """
        raise NotImplementedError()

    def iter_roadmap(self, departure, arrival, filters):
        """
        Get a roadmap.

        @param departure [str]  name of departure station
        @param arrival [str]  name of arrival station
        @param filters [RoadmapFilters]  filters on search
        @return [iter(RoadStep)]  steps of roadmap
        """
        raise NotImplementedError()
