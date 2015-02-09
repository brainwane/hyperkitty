# -*- coding: utf-8 -*-

# Copyright (C) 2011-2015 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.
#
# Author: Aurelien Bompard <abompard@fedoraproject.org>

"""
Import the content of a mbox file into the database.
"""

from __future__ import absolute_import, print_function, unicode_literals

import mailbox
import os
import re
import urllib
import logging
from optparse import make_option
from email.utils import unquote
from traceback import print_exc


from dateutil.parser import parse as parse_date
from dateutil import tz
from django.conf import settings
from django.db import transaction, Error as DatabaseError
from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import utc

from hyperkitty.lib.incoming import add_to_list
from hyperkitty.lib.mailman import sync_with_mailman
from hyperkitty.lib.analysis import compute_thread_order_and_depth
from hyperkitty.models import Email, Attachment, Thread

#from kittystore import SchemaUpgradeNeeded
#from kittystore.scripts import get_store_from_options, StoreFromOptionsError
#from kittystore.caching import sync_mailman
#from kittystore.search import make_delayed


PREFIX_RE = re.compile("^\[([\w\s_-]+)\] ")

ATTACHMENT_RE = re.compile(r"""
--------------[ ]next[ ]part[ ]--------------\n
A[ ]non-text[ ]attachment[ ]was[ ]scrubbed\.\.\.\n
Name:[ ]([^\n]+)\n
Type:[ ]([^\n]+)\n
Size:[ ]\d+[ ]bytes\n
Desc:[ ].+?\n
U(?:rl|RL)[ ]?:[ ]([^\s]+)\s*\n
""", re.X | re.S)

EMBEDDED_MSG_RE = re.compile(r"""
--------------[ ]next[ ]part[ ]--------------\n
An[ ]embedded[ ]message[ ]was[ ]scrubbed\.\.\.\n
From:[ ].+?\n
Subject:[ ](.+?)\n
Date:[ ][^\n]+\n
Size:[ ]\d+\n
U(?:rl|RL)[ ]?:[ ]([^\s]+)\s*\n
""", re.X | re.S)

HTML_ATTACH_RE = re.compile(r"""
--------------[ ]next[ ]part[ ]--------------\n
An[ ]HTML[ ]attachment[ ]was[ ]scrubbed\.\.\.\n
URL:[ ]([^\s]+)\s*\n
""", re.X)

TEXT_NO_CHARSET_RE = re.compile(r"""
--------------[ ]next[ ]part[ ]--------------\n
An[ ]embedded[ ]and[ ]charset-unspecified[ ]text[ ]was[ ]scrubbed\.\.\.\n
Name:[ ]([^\n]+)\n
U(?:rl|RL)[ ]?:[ ]([^\s]+)\s*\n
""", re.X | re.S)

TEXTWRAP_RE = re.compile("\n\s*")



class DownloadError(Exception): pass


class DbImporter(object):
    """
    Import email messages into the KittyStore database using its API.
    """

    def __init__(self, list_address, options):
        self.list_address = list_address
        self.no_download = options["no_download"]
        self.verbose = options["verbosity"] >= 2
        self.since = options.get("since")
        self.total_imported = 0

    def _is_too_old(self, message):
        if not self.since:
            return False
        date = message.get("date")
        if not date:
            return False
        try:
            date = parse_date(date)
        except ValueError, e:
            print("Can't parse date string in message %s: %s"
                  % (message["message-id"], date))
            print(e)
            return False
        if date.tzinfo is None:
            date = date.replace(tzinfo=utc)
        return date < self.since

    def from_mbox(self, mbfile):
        """
        Insert all the emails contained in an mbox file into the database.

        :arg mbfile: a mailbox file
        """
        # TODO: search index
        #self.store.search_index = make_delayed(self.store.search_index)
        mbox = mailbox.mbox(mbfile)
        total_in_mbox = len(mbox)
        cnt_imported = 0
        cnt_read = 0
        for message in mbox:
            if self._is_too_old(message):
                continue
            cnt_read += 1
            self.total_imported += 1
            if self.verbose:
                print("%s (%d/%d)" % (message["Message-Id"], self.total_imported, total_in_mbox))
            # Un-wrap the subject line if necessary
            if message["subject"]:
                message.replace_header("subject",
                        TEXTWRAP_RE.sub(" ", message["subject"]))
            # Parse message to search for attachments
            try:
                attachments = self.extract_attachments(message)
            except DownloadError, e:
                print("Could not download one of the attachments! "
                      "Skipping this message. Error: %s" % e.args[0])
                continue
            # Now insert the message
            try:
                add_to_list(self.list_address, message)
            except ValueError, e:
                if len(e.args) != 2:
                    raise # Regular ValueError exception
                try:
                    print("%s from %s about %s" % (e.args[0],
                            e.args[1].get("From"), e.args[1].get("Subject")))
                except UnicodeDecodeError:
                    print("%s with message-id %s" % (
                            e.args[0], e.args[1].get("Message-ID")))
                continue
            except DatabaseError:
                print_exc()
                print("Message %s failed to import, skipping"
                      % unquote(message["Message-Id"]))
                #if not transaction.get_autocommit():
                #    transaction.rollback()
                continue
            email = Email.objects.get(
                mailinglist__name=self.list_address,
                message_id=unquote(message["message-id"]))
            # And insert the attachments
            for counter, att in enumerate(attachments):
                att["counter"] = counter
                att["email"] = email
                Attachment.objects.create(**att)
            ## Commit every time to be able to rollback on error
            #if not transaction.get_autocommit():
            #    transaction.commit()
            cnt_imported += 1
        #self.store.search_index.flush() # Now commit to the search index
        if self.verbose:
            print('  %s email read' % cnt_read)
            print('  %s email added to the database' % cnt_imported)

    def extract_attachments(self, message):
        """Parse message to search for attachments"""
        all_attachments = []
        message_text = message.as_string()
        #has_attach = False
        #if "-------------- next part --------------" in message_text:
        #    has_attach = True
        # Regular attachments
        attachments = ATTACHMENT_RE.findall(message_text)
        for att in attachments:
            all_attachments.append({
                "name": att[0], "content_type": att[1],
                "content": self.download_attachment(att[2]),
                })
        # Embedded messages
        embedded = EMBEDDED_MSG_RE.findall(message_text)
        for att in embedded:
            all_attachments.append({
                "name": att[0], "content_type": 'message/rfc822',
                "content": self.download_attachment(att[1]),
                })
        # HTML attachments
        html_attachments = HTML_ATTACH_RE.findall(message_text)
        for att in html_attachments:
            url = att.strip("<>")
            all_attachments.append({
                "name": os.path.basename(url), "content_type": 'text/html',
                "content": self.download_attachment(url),
                })
        # Text without charset
        text_no_charset = TEXT_NO_CHARSET_RE.findall(message_text)
        for att in text_no_charset:
            all_attachments.append({
                "name": att[0], "content_type": 'text/plain',
                "content": self.download_attachment(att[1]),
                })
        ## Other, probably inline text/plain
        #if has_attach and not (attachments or embedded
        #                       or html_attachments or text_no_charset):
        #    print message_text
        return all_attachments

    def download_attachment(self, url):
        url = url.strip(" <>")
        if self.no_download:
            if self.verbose:
                print("NOT downloading attachment from %s" % url)
            content = ""
        else:
            if self.verbose:
                print("Downloading attachment from %s" % url)
            try:
                content = urllib.urlopen(url).read()
            except IOError, e:
                raise DownloadError(e)
        return content



class Command(BaseCommand):
    args = "-l <list_address> <mbox> [mbox ...]"
    help = "Imports the specified mailbox archive"
    option_list = BaseCommand.option_list + (
        make_option('-l', '--list-address',
            help="the full list address the mailbox will be imported to"),
        make_option('--no-download',
            action='store_true', default=False,
            help="do not download attachments"),
        make_option('--no-sync-mailman',
            action='store_true', default=False,
            help="do not sync properties with Mailman (faster, useful "
                 "for batch imports)"),
        make_option('--since',
            help="only import emails later than this date")
        )

    def _check_options(self, args, options):
        if not options.get("list_address"):
            raise CommandError(
                "The list address must be given on the command-line.")
        if "@" not in options["list_address"]:
            raise CommandError(
                "The list address must be fully-qualified, including "
                "the '@' symbol and the domain name.")
        if not args:
            raise CommandError("No mbox file selected.")
        for mbfile in args:
            if not os.path.exists(mbfile):
                raise CommandError("No such file: %s" % mbfile)
        options["verbosity"] = int(options.get("verbosity", "1"))
        if options["since"]:
            try:
                options["since"] = parse_date(options["since"])
                if options["since"].tzinfo is None:
                    options["since"] = options["since"].replace(
                        tzinfo=tz.tzlocal())
                if options["verbosity"] >= 2:
                    self.stdout.write("Only emails after %s will be imported"
                                     % options["since"])
            except ValueError, e:
                raise CommandError("invalid value for '--since': %s" % e)

    def handle(self, *args, **options):
        self._check_options(args, options)
        # logging
        if options["verbosity"] >= 3:
            debuglevel = logging.DEBUG
        else:
            debuglevel = logging.INFO
        logging.basicConfig(format='%(message)s', level=debuglevel)
        # main
        list_address = options["list_address"]
        ## Keep autocommit on SQLite:
        ## https://docs.djangoproject.com/en/1.6/topics/db/transactions/#savepoints-in-sqlite
        #if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
        #    transaction.set_autocommit(False)
        settings.HYPERKITTY_BATCH_MODE = True
        importer = DbImporter(list_address, options)
        # disable mailman client for now
        for mbfile in args:
            if options["verbosity"] >= 1:
                self.stdout.write("Importing from mbox file %s to %s"
                                  % (mbfile, list_address))
            importer.from_mbox(mbfile)
            #from hyperkitty.lib.incoming import showtimes
            #showtimes()
            if options["verbosity"] >= 2:
                total_in_list = Email.objects.filter(
                    mailinglist__name=list_address).count()
                self.stdout.write('  %s emails are stored into the database'
                                  % total_in_list)
        if options["verbosity"] >= 1:
            self.stdout.write("Computing thread structure")
        for thread in Thread.objects.all():
            compute_thread_order_and_depth(thread)
        if not options["no_sync_mailman"]:
            if options["verbosity"] >= 1:
                self.stdout.write("Synchronizing properties with Mailman")
            sync_with_mailman()
            #if not transaction.get_autocommit():
            #    transaction.commit()