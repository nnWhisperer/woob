# -*- coding: utf-8 -*-

# Copyright(C) 2010-2012 Romain Bignon
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

from PyQt4.QtGui import QListWidgetItem, QImage, QPixmap, QLabel
from PyQt4.QtCore import SIGNAL, Qt

from weboob.tools.application.qt import QtMainWindow, QtDo, HTMLDelegate
from weboob.tools.application.qt.backendcfg import BackendCfg
from weboob.capabilities.housing import ICapHousing, Query, City
from weboob.capabilities.base import NotLoaded

from .ui.main_window_ui import Ui_MainWindow
from .query import QueryDialog

class MainWindow(QtMainWindow):
    def __init__(self, config, weboob, parent=None):
        QtMainWindow.__init__(self, parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.config = config
        self.weboob = weboob
        self.process = None
        self.housing = None
        self.displayed_photo_idx = 0
        self.process_photo = {}

        self.ui.housingsList.setItemDelegate(HTMLDelegate())
        self.ui.housingFrame.hide()

        self.connect(self.ui.actionBackends, SIGNAL("triggered()"), self.backendsConfig)
        self.connect(self.ui.queriesList, SIGNAL('currentIndexChanged(int)'), self.queryChanged)
        self.connect(self.ui.addQueryButton, SIGNAL('clicked()'), self.addQuery)
        self.connect(self.ui.housingsList, SIGNAL('itemClicked(QListWidgetItem*)'), self.housingSelected)
        self.connect(self.ui.previousButton, SIGNAL('clicked()'), self.previousClicked)
        self.connect(self.ui.nextButton, SIGNAL('clicked()'), self.nextClicked)

        self.reloadQueriesList()
        self.refreshHousingsList()

        if self.weboob.count_backends() == 0:
            self.backendsConfig()

    def backendsConfig(self):
        bckndcfg = BackendCfg(self.weboob, (ICapHousing,), self)
        if bckndcfg.run():
            pass

    def reloadQueriesList(self, select_name=None):
        self.disconnect(self.ui.queriesList, SIGNAL('currentIndexChanged(int)'), self.queryChanged)
        self.ui.queriesList.clear()
        for name in self.config.get('queries', default={}).iterkeys():
            self.ui.queriesList.addItem(name)
            if name == select_name:
                self.ui.queriesList.setCurrentIndex(len(self.ui.queriesList))
        self.connect(self.ui.queriesList, SIGNAL('currentIndexChanged(int)'), self.queryChanged)

    def addQuery(self):
        querydlg = QueryDialog(self.weboob, self)
        if querydlg.exec_():
            name = unicode(querydlg.ui.nameEdit.text())
            query = {}
            query['cities'] = []
            for i in xrange(len(querydlg.ui.citiesList)):
                item = querydlg.ui.citiesList.item(i)
                city = item.data(Qt.UserRole).toPyObject()
                query['cities'].append({'id': city.id, 'backend': city.backend, 'name': city.name})
            query['area_min'] = querydlg.ui.areaMin.value()
            query['area_max'] = querydlg.ui.areaMax.value()
            query['cost_min'] = querydlg.ui.costMin.value()
            query['cost_max'] = querydlg.ui.costMax.value()
            self.config.set('queries', name, query)
            self.config.save()

            self.reloadQueriesList(name)

    def queryChanged(self, i):
        self.refreshHousingsList()

    def refreshHousingsList(self):
        name = unicode(self.ui.queriesList.itemText(self.ui.queriesList.currentIndex()))
        q = self.config.get('queries', name)

        self.ui.housingsList.clear()
        self.ui.queriesList.setEnabled(False)

        query = Query()
        query.cities = []
        for c in q['cities']:
            city = City(c['id'])
            city.backend = c['backend']
            city.name = c['name']
            query.cities.append(city)

        query.area_min = int(q['area_min']) or None
        query.area_max = int(q['area_max']) or None
        query.cost_min = int(q['cost_min']) or None
        query.cost_max = int(q['cost_max']) or None

        self.process = QtDo(self.weboob, self.addHousing)
        self.process.do('search_housings', query)

    def addHousing(self, backend, housing):
        if not backend:
            self.ui.queriesList.setEnabled(True)
            self.process = None
            return

        item = QListWidgetItem()
        item.setText(u'<h2>%s</h2><i>%s — %s%s (%s)</i><br />%s' % (housing.title, housing.date.strftime('%Y-%m-%d') if housing.date else 'Unknown',
                                                        housing.cost, housing.currency, housing.backend, housing.text))
        item.setData(Qt.UserRole, housing)
        self.ui.housingsList.addItem(item)

    def housingSelected(self, item):
        housing = item.data(Qt.UserRole).toPyObject()
        self.ui.queriesFrame.setEnabled(False)

        self.setHousing(housing)

        self.process = QtDo(self.weboob, self.gotHousing)
        self.process.do('fillobj', housing, backends=housing.backend)

    def setHousing(self, housing, nottext='Loading...'):
        self.housing = housing

        self.ui.housingFrame.show()

        self.display_photo()

        self.ui.titleLabel.setText('<h1>%s</h1>' % housing.title)
        self.ui.areaLabel.setText(u'%s m²' % housing.area)
        self.ui.costLabel.setText(u'%s %s' % (housing.cost, housing.currency))
        self.ui.dateLabel.setText(housing.date.strftime('%Y-%m-%d') if housing.date else nottext)
        self.ui.phoneLabel.setText(housing.phone or nottext)
        self.ui.locationLabel.setText(housing.location or nottext)
        self.ui.stationLabel.setText(housing.station or nottext)

        self.ui.descriptionEdit.setText(housing.text or nottext)

        while self.ui.detailsFrame.layout().count() > 0:
            child = self.ui.detailsFrame.layout().takeAt(0)
            child.widget().hide()
            child.widget().deleteLater()

        if housing.details:
            for key, value in housing.details.iteritems():
                label = QLabel(value)
                label.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.LinksAccessibleByMouse)
                self.ui.detailsFrame.layout().addRow('<b>%s:</b>' % key, label)

    def gotHousing(self, backend, housing):
        if not backend:
            self.ui.queriesFrame.setEnabled(True)
            self.process = None
            return

        self.setHousing(housing, nottext='')

    def previousClicked(self):
        if len(self.housing.photos) == 0:
            return
        self.displayed_photo_idx = (self.displayed_photo_idx - 1) % len(self.housing.photos)
        self.display_photo()

    def nextClicked(self):
        if len(self.housing.photos) == 0:
            return
        self.displayed_photo_idx = (self.displayed_photo_idx + 1) % len(self.housing.photos)
        self.display_photo()

    def display_photo(self):
        if not self.housing.photos:
            self.ui.photoUrlLabel.setText('')
            return

        if self.displayed_photo_idx >= len(self.housing.photos):
            self.displayed_photo_idx = len(self.housing.photos) - 1
        if self.displayed_photo_idx < 0:
            self.ui.photoUrlLabel.setText('')
            return

        photo = self.housing.photos[self.displayed_photo_idx]
        if photo.data:
            data = photo.data
            if photo.id in self.process_photo:
                self.process_photo.pop(photo.id)
        else:
            self.process_photo[photo.id] = QtDo(self.weboob, lambda b,p: self.display_photo())
            self.process_photo[photo.id].do('fillobj', photo, ['data'], backends=self.housing.backend)

            if photo.thumbnail_data:
                data = photo.thumbnail_data
            else:
                return

        img = QImage.fromData(data)
        img = img.scaledToWidth(self.width()/3)

        self.ui.photoLabel.setPixmap(QPixmap.fromImage(img))
        if photo.url is not NotLoaded:
            text = '<a href="%s">%s</a>' % (photo.url, photo.url)
            self.ui.photoUrlLabel.setText(text)
