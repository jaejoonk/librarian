# -*- mode: python; coding: utf-8 -*-
# Copyright 2019 the HERA Collaboration
# Licensed under the 2-clause BSD License

"""Test code in librarian_server/search.py

"""

import pytest

from librarian_server import search
from librarian_server.webutil import ServerError


class TestGenericSearchCompiler(object):
    """Tests for the GenericSearchCompiler object"""
    def test_compile(self):
        gsc = search.GenericSearchCompiler()
        bogus_search = "foo"
        with pytest.raises(ServerError):
            gsc.compile(bogus_search)
