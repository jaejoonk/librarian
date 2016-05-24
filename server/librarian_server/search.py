# -*- mode: python; coding: utf-8 -*-
# Copyright 2016 the HERA Collaboration
# Licensed under the BSD License.

"""Searches of the database

This code will likely need a lot of expansion, but we'll start simple.

"""

from __future__ import absolute_import, division, print_function, unicode_literals

__all__ = str('''
select_files
StandingOrder
queue_standing_order_copies
''').split ()

import datetime, logging, os.path, time

from flask import flash, redirect, render_template, request, url_for

from . import app, db
from .dbutil import NotNull
from .webutil import ServerError, json_api, login_required, optional_arg, required_arg


def select_files (search_string):
    from .file import File

    if search_string == 'special-test-1':
        two_weeks_ago = datetime.datetime.utcnow () - datetime.timedelta (days=14)
        return File.query.filter (File.create_time > two_weeks_ago,
                                  File.name.like ('%22130%'))
    elif search_string == 'special-test-2':
        two_weeks_ago = datetime.datetime.utcnow () - datetime.timedelta (days=14)
        return File.query.filter (File.create_time > two_weeks_ago,
                                  File.name.like ('zen%HH.uvc'))
    elif search_string == 'empty-search':
        return File.query.filter (File.size != File.size)

    raise NotImplementedError ('general searching not actually implemented')


# "Standing orders" to copy files from one Librarian to another.

stord_logger = logging.getLogger ('librarian.standingorders')


class StandingOrder (db.Model):
    """A StandingOrder describes a rule for copying data from this Librarian to
    another. We save a search and a destination. When new files match that
    search, we automatically start copying them to the destination. We create
    a FileEvent with a name based on the name of the StandingOrder to mark
    when a file has successfully been copied.

    It is assumed that the relevant search has some time limit applied so that
    only files created in the last (e.g.) 7 days match.

    """
    __tablename__ = 'standing_order'

    id = db.Column (db.Integer, primary_key=True, autoincrement=True)
    name = NotNull (db.String (64), unique=True)
    search = NotNull (db.Text)
    conn_name = NotNull (db.String (64))

    def __init__ (self, name, search, conn_name):
        self.name = name
        self.search = search
        self.conn_name = conn_name
        self._validate ()


    def _validate (self):
        """Check that this object's fields follow our invariants.

        """
        # TODO: validate the search string
        pass


    @property
    def event_type (self):
        return 'standing_order_succeeded:' + self.name


    def get_files_to_copy (self):
        """Generate a list of files that ought to be copied, according to the
        specifications of this StandingOrder.

        """
        from .file import File, FileEvent

        # The core query is something freeform specified by the user.

        query = select_files (self.search)

        # We then layer on a check that the files don't have the specified
        # marker event.

        already_done = (db.session.query (File.name)
                        .filter (FileEvent.name == File.name,
                                 FileEvent.type == self.event_type))
        query = query.filter (~File.name.in_ (already_done))

        # Finally we filter out files that already have copy tasks associated
        # with this standing order, exceping those tasks that encountered an
        # error.

        from .store import UploaderTask
        from .bgtasks import the_task_manager

        already_launched = set (os.path.basename (t.store_path)
                                for t in the_task_manager.tasks
                                if (isinstance (t, UploaderTask) and
                                    self.name == t.standing_order_name and
                                    t.exception is None))

        for file in query:
            if file.name not in already_launched:
                yield file


    def maybe_launch_copies (self):
        """Launch any file copy operations that need to happen according to this
        StandingOrder's specification.

        """
        from .store import launch_copy_by_file_name
        stord_logger.debug ('evaluating standing order %s', self.name)

        for file in self.get_files_to_copy ():
            stord_logger.debug ('got a hit: %s', file.name)
            if launch_copy_by_file_name (file.name, self.conn_name,
                                         standing_order_name=self.name, no_instance='return'):
                stord_logger.warn ('standing order %s should copy file %s to %s, but no instances '
                                   'of it are available', self.name, file.name, self.conn_name)


# A simple little manager for running standing orders. We have a timeout to
# not evaluate them that often, although doing so shouldn't be too expensive.

MIN_STANDING_ORDER_INTERVAL = 300 # seconds
DEFAULT_STANDING_ORDER_DELAY = 90 # seconds


def _launch_copy_timeout ():
    stord_logger.debug ('timeout invoked')

    if the_standing_order_manager.maybe_launch_copies ():
        # The checks actually ran.
        the_standing_order_manager.launch_queued = False
    else:
        # We didn't run the checks because we did so recently. If a new file
        # was uploaded we want to make sure that it's eventually checked, so
        # re-queue ourselves to run again.
        from tornado.ioloop import IOLoop
        stord_logger.debug ('re-scheduling timeout')
        IOLoop.instance ().call_later (DEFAULT_STANDING_ORDER_DELAY, _launch_copy_timeout)


class StandingOrderManager (object):
    """A simple, singleton class for managing our standing orders.

    Other folks should primarily access the manager via the
    `queue_standing_order_copies` function. That function *queues* a command
    to examine our standing orders and launch any needed copy commands, with a
    default delay of 90 seconds. The delay is in place since uploads of files
    to the Librarian are likely to occur in batches, but it's easiest to just
    command the manager to "do its thing" whenever a file is uploaded. The
    delay makes it so that when we actually look for files to copy, there's
    probably a bunch of them ready to go, not just the very first one that was
    uploaded.

    """
    last_check = 0
    launch_queued = False

    def maybe_launch_copies (self):
        """Returns True unless nothing happened because we've run a search recently.

        """
        now = time.time ()

        if now - self.last_check < MIN_STANDING_ORDER_INTERVAL:
            return False # Don't evaluate too often

        stord_logger.debug ('running searches')
        self.last_check = now

        for storder in StandingOrder.query.all ():
            storder.maybe_launch_copies ()

        return True


    def queue_launch_copy (self):
        stord_logger.debug ('called queue_launch_copy')
        if self.launch_queued:
            return

        self.launch_queued = True
        from tornado.ioloop import IOLoop
        stord_logger.debug ('timeout actually scheduled')
        IOLoop.instance ().call_later (DEFAULT_STANDING_ORDER_DELAY, _launch_copy_timeout)


the_standing_order_manager = StandingOrderManager ()

def queue_standing_order_copies ():
    the_standing_order_manager.queue_launch_copy ()


# Web user interface

@app.route ('/standing-orders')
@login_required
def standing_orders ():
    q = StandingOrder.query.order_by (StandingOrder.name.asc ())

    return render_template (
        'standing-order-listing.html',
        title='Standing Orders',
        storders=q,
    )


@app.route ('/standing-orders/<string:name>')
@login_required
def specific_standing_order (name):
    storder = StandingOrder.query.filter (StandingOrder.name == name).first ()
    if storder is None:
        flash ('No such standing order "%s"' % name)
        return redirect (url_for ('standing_orders'))

    try:
        cur_files = list (storder.get_files_to_copy ())
    except Exception as e:
        flash ('Cannot run this order’s search: %s' % e)
        cur_files = []

    return render_template (
        'standing-order-individual.html',
        title='Standing Order %s' % (storder.name),
        storder=storder,
        cur_files=cur_files,
    )


@app.route ('/standing-orders/<string:ignored_name>/create', methods=['POST'])
@login_required
def create_standing_order (ignored_name):
    """Note that we ignore the order name and instead takes its value from the
    POST data; this is basically an implementation/consistency thing.

    """
    name = required_arg (request.form, unicode, 'name')

    try:
        if not len (name):
            raise Exception ('order name may not be empty')

        storder = StandingOrder (name, 'empty-search', 'undefined-connection')
        storder._validate ()
        db.session.add (storder)
        db.session.commit ()
    except Exception as e:
        flash ('Cannot create "%s": %s' % (name, e))
        return redirect (url_for ('standing_orders'))

    return redirect (url_for ('standing_orders') + '/' + name)


@app.route ('/standing-orders/<string:name>/update', methods=['POST'])
@login_required
def update_standing_order (name):
    storder = StandingOrder.query.filter (StandingOrder.name == name).first ()
    if storder is None:
        flash ('No such standing order "%s"' % name)
        return redirect (url_for ('standing_orders'))

    new_name = required_arg (request.form, unicode, 'name')
    new_conn = required_arg (request.form, unicode, 'conn')
    new_search = required_arg (request.form, unicode, 'search')

    try:
        storder.name = new_name
        storder.conn_name = new_conn
        storder.search = new_search
        storder._validate ()
        db.session.merge (storder)
        db.session.commit ()
    except Exception as e:
        flash ('Cannot update "%s": %s' % (name, e))
        return redirect (url_for ('standing_orders'))

    flash ('Updated standing order "%s"' % new_name)
    return redirect (url_for ('standing_orders'))


@app.route ('/standing-orders/<string:name>/delete', methods=['POST'])
@login_required
def delete_standing_order (name):
    storder = StandingOrder.query.filter (StandingOrder.name == name).first ()
    if storder is None:
        flash ('No such standing order "%s"' % name)
        return redirect (url_for ('standing_orders'))

    db.session.delete (storder)
    db.session.commit ()

    flash ('Deleted standing order "%s"' % name)
    return redirect (url_for ('standing_orders'))
