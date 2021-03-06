#!/usr/bin/env python
#
# Copyright 2014 Mimetic Markets, Inc.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

from sqlalchemy.orm.exc import NoResultFound
import sys
import os

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
    "../server"))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
    "../dist/config"))

from sputnik import database, models
from sputnik import txbitcoinrpc
import getpass
from sputnik import config
from twisted.internet import defer, reactor, task

db_session = database.make_session(username=getpass.getuser())
print config.get("cashier","bitcoin_conf")
conn = txbitcoinrpc.BitcoinRpc(config.get("cashier", "bitcoin_conf"))

#conn.walletpassphrase('pass',10, dont_raise=True)
count = 0
def go():
    d = conn.keypoolrefill()

    def get_addresses(result):
        quantity = 100

        dl = defer.DeferredList([conn.getnewaddress() for i in range(quantity)])

        def add_addresses(results):
            for r in results:
                addr = r[1]['result']
                BTC = db_session.query(models.Contract).filter_by(ticker='BTC').one()
                new_address = models.Addresses(None, BTC, addr)
                db_session.add(new_address)
                print 'adding: ', addr
            db_session.commit()
            print 'committed'
            reactor.stop()

        dl.addCallback(add_addresses)
        return dl

    def try_again(failure):
        print "Error: %s" % str(failure.value)
        global count
        count += 1
        if count > 10:
            reactor.stop()
            raise failure.value
        else:
            return task.deferLater(reactor, 30, go)

    d.addCallback(get_addresses)
    d.addErrback(try_again)
    return d

go()
reactor.run()
