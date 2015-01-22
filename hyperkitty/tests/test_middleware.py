# -*- coding: utf-8 -*-
# Copyright (C) 1998-2015 by the Free Software Foundation, Inc.
#
# This file is part of HyperKitty.
#
# HyperKitty is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# HyperKitty is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# HyperKitty.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Aamir Khan <syst3m.w0rm@gmail.com>
# Author: Aurelien Bompard <abompard@fedoraproject.org>
#


from urllib2 import HTTPError
from pprint import pprint
from uuid import UUID
from mock import Mock

from hyperkitty.tests.utils import TestCase
from hyperkitty.middleware import MailmanUserMetadata
from mailmanclient import MailmanConnectionError


class MailmanMiddlewareTestCase(TestCase):

    def setUp(self):
        self.middleware = MailmanUserMetadata()
        self.view_func = Mock()
        self.request = Mock()
        self.request.user.is_authenticated.return_value = True
        self.request.user.email = "testuser@example.com"
        self.request.session = {}


    def test_no_connection(self):
        self.middleware.process_view(self.request, self.view_func, [], {})
        try:
            self.middleware.process_view(self.request, self.view_func, [], {})
        except MailmanConnectionError:
            self.fail("MailmanConnectionError was raised")

    def test_setting_user_id(self):
        self.set_mailman_client_mode("mocking")
        self.mailman_client.create_user("testuser@example.com", "pw")
        user = self.mailman_client.get_user("testuser@example.com")
        expected_user_id = UUID(int=user.user_id)
        self.middleware.process_view(self.request, self.view_func, [], {})
        #print self.mailman_client.called_paths
        self.assertTrue("user_id" in self.request.session)
        self.assertEqual(self.request.session["user_id"], expected_user_id)

    def test_setting_subscriptions(self):
        self.set_mailman_client_mode("mocking")
        dom = self.mailman_client.create_domain("example.com")
        ml = dom.create_list("testlist")
        ml.subscribe("testuser@example.com")
        self.middleware.process_view(self.request, self.view_func, [], {})
        #print self.mailman_client.called_paths
        self.assertTrue("subscribed" in self.request.session)
        self.assertEqual(self.request.session["subscribed"],
                         ["testlist@example.com"])
