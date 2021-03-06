import sys

import zerorpc
from time import sleep
from gevent import Greenlet

from ..log import get_logger
log = get_logger()

def make_zerorpc(cls, location):
    assert location, "Location to bind for %s cannot be none!" % cls
    def m():
        """ Exposes the given class as a ZeroRPC server on the given address+port """
        s = zerorpc.Server(cls())
        s.bind(location)
        log.info("ZeroRPC: Starting %s at %s" % (cls.__name__, location))
        s.run()
    return Greenlet.spawn(m)

def print_dots():
    """This Greenlet prints dots to the console which is useful for making
    sure that other greenlets are properly not blocking."""
    def m():
        while True:
            sys.stdout.write("."),
            sys.stdout.flush()
            sleep(.02)
    Greenlet.spawn(m)
