# -*- mode: python; coding: utf-8 -*-
# Copyright 2016 the HERA Collaboration
# Licensed under the BSD License.

"""Stores.

So this gets a bit complicated. The `hera_librarian package`, which is used by
both the server and clients, includes a Store class, since Librarian clients
access stores directly by SSH'ing into them. However, here in the server, we
also have database records for every store. I *think* it will not make things
too complicated and crazy to do the multiple inheritance thing we do below, so
that we get the functionality of the `hera_librarian.store.Store` class while
also making our `ServerStore` objects use the SQLAlchemy ORM. If this turns
out to be a dumb idea, we should have the ORM-Store class just be a thin
wrapper that can easily be turned into a `hera_librarian.store.Store`
instance.

"""

from __future__ import absolute_import, division, print_function, unicode_literals

__all__ = str('''
Store
''').split ()

import os.path

from flask import render_template

from hera_librarian.store import Store as BaseStore

from . import app, db
from .dbutil import NotNull
from .webutil import ServerError, json_api, login_required, optional_arg, required_arg


class Store (db.Model, BaseStore):
    """A Store is a computer with a disk where we can store data. Several of the
    things we keep track of regarding stores are essentially configuration
    items; but we also keep track of the machine's availability, which is
    state that is better tracked in the database.

    """
    __tablename__ = 'store'

    id = db.Column (db.Integer, primary_key=True)
    name = NotNull (db.String (256))
    ssh_host = NotNull (db.String (256))
    path_prefix = NotNull (db.String (256))
    http_prefix = db.Column (db.String (256))
    available = NotNull (db.Boolean)

    def __init__ (self, name, path_prefix, ssh_host):
        db.Model.__init__ (self)
        BaseStore.__init__ (self, name, path_prefix, ssh_host)
        self.available = True


    @classmethod
    def get_by_name (cls, name):
        """Look up a store by name, or raise an ServerError on failure."""

        stores = list (cls.query.filter (cls.name == name))
        if not len (stores):
            raise ServerError ('No such store %r', name)
        if len (stores) > 1:
            raise ServerError ('Internal error: multiple stores with name %r', name)
        return stores[0]


# RPC API

@app.route ('/api/recommended_store', methods=['GET', 'POST'])
@json_api
def recommended_store (args, sourcename=None):
    file_size = required_arg (args, int, 'file_size')
    if file_size < 0:
        raise ServerError ('"file_size" must be nonnegative')

    # We are simpleminded and just choose the store with the most available
    # space.

    most_avail = -1
    most_avail_store = None

    for store in Store.query.filter (Store.available):
        avail = store.get_space_info ()['available']
        if avail > most_avail:
            most_avail = avail
            most_avail_store = store

    if most_avail < file_size or most_avail_store is None:
        raise ServerError ('unable to find a store able to hold %d bytes', file_size)

    info = {}
    info['name'] = store.name
    info['ssh_host'] = store.ssh_host
    info['path_prefix'] = store.path_prefix
    info['available'] = most_avail # might be helpful?
    return info


@app.route ('/api/complete_upload', methods=['GET', 'POST'])
@json_api
def complete_upload (args, sourcename=None):
    """Called after a Librarian client has finished uploading a file instance to
    one of our Stores. We verify that the upload was successful and move the
    file into its final destination.

    This function is paired with hera_librarian.store.Store.stage_file_on_store.

    """
    store_name = required_arg (args, unicode, 'store_name')
    expected_size = required_arg (args, int, 'size')
    expected_md5 = required_arg (args, unicode, 'md5')
    obsid = required_arg (args, int, 'obsid')
    start_jd = required_arg (args, float, 'start_jd')
    dest_store_path = required_arg (args, unicode, 'dest_store_path')
    create_time = optional_arg (args, int, 'create_time_unix')

    if create_time is not None:
        import datetime
        create_time = datetime.datetime.fromtimestamp (create_time)

    store = Store.get_by_name (store_name) # ServerError if failure
    stage_path = 'upload_%s_%s.staging' % (expected_size, expected_md5)

    # Validate the staged file, abusing our argument-parsing helpers to make
    # sure we got everything from the info call:

    #try:
    info = store.get_info_for_path (stage_path)
    #except Exception as e:
    #    raise ServerError ('cannot complete upload to %s:%s: %s',
    #                       store_name, dest_store_path, e)

    observed_type = required_arg (info, unicode, 'type')
    observed_size = required_arg (info, int, 'size')
    observed_md5 = required_arg (info, unicode, 'md5')

    if observed_size != expected_size:
        raise ServerError ('cannot complete upload to %s:%s: expected size %d; observed %d',
                           store_name, dest_store_path, expected_size, observed_size)

    if observed_md5 != expected_md5:
        raise ServerError ('cannot complete upload to %s:%s: expected MD5 %s; observed %s',
                           store_name, dest_store_path, expected_md5, observed_md5)

    # Do we already have the intended instance? If so ... just delete the
    # staged instance and return success, because the intended effect of this
    # RPC call has already been achieved.

    parent_dirs = os.path.dirname (dest_store_path)
    name = os.path.basename (dest_store_path)

    from .file import File, FileInstance
    instance = FileInstance.query.get ((store.id, parent_dirs, name))
    if instance is not None:
        store._delete (stage_path)
        return {}

    # Staged file is OK and we're not redundant. Move it to its new home.

    store._move (stage_path, dest_store_path)

    # Finally, update the database.

    from .observation import Observation

    obs = Observation (obsid, start_jd, None, None)
    file = File (name, observed_type, obsid, sourcename, observed_size, observed_md5, create_time)
    inst = FileInstance (store, parent_dirs, name)
    db.session.merge (obs)
    db.session.merge (file)
    db.session.merge (inst)
    db.session.commit ()

    return {}


# Web user interface

@app.route ('/stores')
@login_required
def stores ():
    q = Store.query.order_by (Store.name.asc ())
    return render_template (
        'store-listing.html',
        title='Stores',
        stores=q
    )


@app.route ('/stores/<string:name>')
@login_required
def specific_store (name):
    try:
        store = Store.get_by_name (name)
    except ServerError as e:
        flash (str (e))
        return redirect (url_for ('stores'))

    from .file import FileInstance
    instances = list (FileInstance.query
                      .filter (FileInstance.store == store.id)
                      .order_by (FileInstance.parent_dirs.asc (),
                                 FileInstance.name.asc ()))

    return render_template (
        'store-individual.html',
        title='Store %s' % (store.name),
        store=store,
        instances=instances,
    )
