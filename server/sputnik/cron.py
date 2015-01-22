#!/usr/bin/env python
__author__ = 'sameer'

import sys

from twisted.python import log
from twisted.internet import reactor

import config
from zmq_util import dealer_proxy_async
import argparse


class Cron:
    def __init__(self, administrator):
        self.administrator = administrator

    def mail_statements(self, period):
        return self.administrator.mail_statements(period)

if __name__ == "__main__":
    log.startLogging(sys.stdout)

    administrator = dealer_proxy_async(config.get("administrator", "cron_export"))
    cron = Cron(administrator)

    # Parse arguments to figure out what to do
    parser = argparse.ArgumentParser(description="Run Sputnik jobs out of cron")
    subparsers = parser.add_subparsers(description="job that is to be performed", metavar="command", dest="command")
    parser_mail_statements = subparsers.add_parser("mail_statements", help="Mail statements to users")
    parser_mail_statements.add_argument("--period", dest="period", action="store", default="monthly",
                                        help="Statement period", choices=["monthly", "weekly", "daily"])

    kwargs = vars(parser.parse_args())
    command = kwargs["command"]
    del kwargs["command"]

    method = getattr(cron, command)

    result = method(**kwargs)
    def _cb(result):
        log.msg("%s result: %s" % (command, result))
        reactor.stop()
    def _err(failure):
        log.err(failure)
        reactor.stop()

    result.addCallback(_cb).addErrback(_err)
    reactor.run()






