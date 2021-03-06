#!/usr/bin/env python
#
# Copyright 2014 Mimetic Markets, Inc.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
"""
.. module:: accountant

The accountant is responsible for user-specific data, except for login sorts of data, which are managed by the
administrator. It is responsible for the following:

* models.Position
* models.PermissionGroup

"""

import sys
from optparse import OptionParser

import config
from rpc_schema import schema

from optparse import OptionParser
from decimal import Decimal

parser = OptionParser()
parser.add_option("-c", "--config", dest="filename",
                  help="config file")
(options, args) = parser.parse_args()
if options.filename:
    config.reconfigure(options.filename)

import database
import models
import margin
import util
import ledger
from alerts import AlertsProxy
from sendmail import Sendmail

from ledger import create_posting

from zmq_util import export, dealer_proxy_async, router_share_async, pull_share_async, \
    push_proxy_async, RemoteCallTimedOut, RemoteCallException, ComponentExport

from twisted.internet import reactor, defer, task
from twisted.python import log
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
from watchdog import watchdog
from jinja2 import Environment, FileSystemLoader

import time
from datetime import datetime
from util import session_aware
from exception import *

INSUFFICIENT_MARGIN = AccountantException("exceptions/accountant/insufficient_margin")
TRADE_NOT_PERMITTED = AccountantException("exceptions/accountant/trade_not_permitted")
WITHDRAW_NOT_PERMITTED = AccountantException("exceptions/accountant/withdraw_not_permitted")
INVALID_CURRENCY_QUANTITY = AccountantException("exceptions/accountant/invalid_currency_quantity")
DISABLED_USER = AccountantException("exceptions/accountant/disabled_user")
CONTRACT_EXPIRED = AccountantException("exceptions/accountant/contract_expired")
CONTRACT_NOT_EXPIRED = AccountantException("exceptions/accountant/contract_not_expired")
NON_CLEARING_CONTRACT = AccountantException("exceptions/accountant/non_clearing_contract")
CONTRACT_CLEARING = AccountantException(9, "exceptions/accountant/contract_clearing")
CONTRACT_NOT_ACTIVE = AccountantException("exceptions/accountant/contract_not_active")
NO_ORDER_FOUND = AccountantException("exceptions/accountant/no_order_found")
USER_ORDER_MISMATCH = AccountantException("exceptions/accountant/user_order_mismatch")
ORDER_CANCELLED = AccountantException("exceptions/accountant/order_cancelled")
WITHDRAWAL_TOO_SMALL = AccountantException("exceptions/accountant/withdrawal_too_small")
NO_SUCH_USER = AccountantException("exceptions/accountant/no_such_user")
INVALID_PRICE_QUANTITY = AccountantException("exceptions/accountant/invalid_price_quantity")
INVALID_CONTRACT_TYPE = AccountantException("exceptions/accountant/invalid_contract_type")

class Accountant:
    """The Accountant primary class

    """
    def __init__(self, session, engines, cashier, ledger, webserver, accountant_proxy,
                 alerts_proxy, accountant_number=0, debug=False, trial_period=False,
                 mimetic_share=0.5, sendmail=None, template_dir='admin_templates'):
        """Initialize the Accountant

        :param session: The SQL Alchemy session
        :type session:
        :param debug: Whether or not weird things can happen like position adjustment
        :type debug: bool

        """

        self.session = session
        self.debug = debug
        self.deposit_limits = {}
        # TODO: Make this configurable
        self.vendor_share_config = { 'm2': mimetic_share,
                                     'customer': 1.0-mimetic_share
        }
        self.safe_prices = {}
        self.engines = engines
        self.ledger = ledger
        self.cashier = cashier
        self.accountant_proxy = accountant_proxy
        self.trial_period = trial_period
        self.alerts_proxy = alerts_proxy
        for contract in self.session.query(models.Contract).filter_by(
                active=True).filter(models.Contract.contract_type != "cash"):
            d = self.engines[contract.ticker].get_safe_price()
            def get_cb(ticker):
                def _cb(safe_price):
                    self.safe_prices[ticker] = safe_price

                return _cb

            d.addCallback(get_cb(contract.ticker))

        self.webserver = webserver
        self.disabled_users = {}
        self.clearing_contracts = {}
        self.accountant_number = accountant_number
        self.jinja_env = Environment(loader=FileSystemLoader(template_dir))
        self.sendmail = sendmail

    def post_or_fail(self, *postings):
        # This is the core ledger communication method.
        # Posting happens as follows:
        # 1. All affected positions have a counter incremented to keep track of
        #    pending postings.
        # 2. The ledger's RPC post() is invoked.
        # 3. When the call returns, the position counters are decremented. This
        #    happens whether or not there was an error.
        # 4a. If there was no error, positions are updated and the webserver is
        #     notified.
        # 4b. If there was an error, an effort is made to determine what caused
        #     it. If the error was severe, send_alert() is called to let us
        #     know. In all cases, the error is propogated downstream to let
        #     whoever called post_or_fail know that the post was not successful.
        # Note: It is *important* that all invocations of post_or_fail attach
        #       an errback (even if it is just log.err) to catch the
        #       propogating error.
        # We initialize positions here if they don't already exist

        def update_counters(increment=False):
            change = 1 if increment else -1

            try:
                for posting in postings:
                    position = self.get_position(
                            posting['username'], posting['contract'])
                    position.pending_postings += change
                    # we might be initializing the position here, so make sure it is in db
                    self.session.add(position)
                self.session.commit()
            except SQLAlchemyError, e:
                log.err("Could not update counters: %s" % e)
                self.alerts_proxy.send_alert("Exception in ledger. See logs.")
                self.session.rollback()
            finally:
                self.session.rollback()

        def on_success(result):
            log.msg("Post success: %s" % result)
            try:
                for posting in postings:
                    position = self.get_position(posting['username'], posting['contract'])
                    user = self.get_user(posting['username'])
                    if posting['direction'] == 'debit':
                        sign = 1 if user.type == 'Asset' else -1
                    else:
                        sign = -1 if user.type == 'Asset' else 1

                    log.msg("Adjusting position %s by %d %s" % (position, posting['quantity'], posting['direction']))
                    position.position += sign * posting['quantity']
                    log.msg("New position: %s" % position)
                    #self.session.merge(position)
                self.session.commit()
            finally:
                self.session.rollback()

        def on_fail_ledger(failure):
            e = failure.trap(ledger.LedgerException)
            log.err("Ledger exception:")
            log.err(failure.value)
            self.alerts_proxy.send_alert("Exception in ledger. See logs.")
            # propogate error downstream
            return failure

        def on_fail_rpc(failure):
            e = failure.trap(RemoteCallException)
            if isinstance(failure.value, RemoteCallTimedOut):
                log.err("Ledger call timed out.")
                self.alerts_proxy.send_alert("Ledger call timed out. Ledger may be overloaded.")
            else:
                log.err("Improper ledger RPC invocation:")
                log.err(failure)
            # propogate error downstream
            return failure

        def on_fail_other(failure):
            log.err("Error in processing posting result. This should be handled downstream.")
            log.err(failure)
            # propogate error downstream
            return failure

        def publish_transactions(result):
            for posting in postings:
                transaction = {'contract': posting['contract'],
                          'timestamp': posting['timestamp'],
                          'quantity': posting['quantity'],
                          'type': posting['type'],
                          'direction': posting['direction'],
                          'note': posting['note']
                }
                self.webserver.transaction(posting['username'], transaction)

        def decrement_counters(result):
            update_counters(increment=False)
            return result

        update_counters(increment=True)

        d = self.ledger.post(*postings)

        d.addBoth(decrement_counters)
        d.addCallback(on_success).addCallback(publish_transactions)
        d.addErrback(on_fail_ledger).addErrback(on_fail_rpc)

        # Just in case there are no error handlers downstream, log any leftover
        # errors here.
        d.addErrback(on_fail_other)

        return d

    def get_user(self, username):
        """Return the User object corresponding to the username.

        :param username: the username to look up
        :type username: str, models.User
        :returns: models.User -- the User matching the username
        :raises: AccountantException
        """

        if isinstance(username, models.User):
            return username

        try:
            return self.session.query(models.User).filter_by(
                username=username).one()
        except NoResultFound:
            raise NO_SUCH_USER

    def get_contract(self, ticker):
        """
        Return the Contract object corresponding to the ticker.
        :param ticker: the ticker to look up or a Contract id
        :type ticker: str, models.Contract
        :returns: models.Contract -- the Contract object matching the ticker
        :raises: AccountantException
        """
        try:
            return util.get_contract(self.session, ticker)
        except:
            raise AccountantException("No such contract: '%s'." % ticker)

    def adjust_position(self, username, ticker, quantity, admin_username):
        """Adjust a user's position, offsetting with the 'adjustment' account

        :param username: The user
        :type username: str, models.User
        :param ticker: The contract
        :type ticker: str, models.Contract
        :param quantity: the delta to apply
        :type quantity: int

        """
        if not self.debug:
            raise AccountantException(0, "Position modification not allowed")

        uid = util.get_uid()
        credit = create_posting("Transfer", username, ticker, quantity,
                "credit", "Adjustment (%s)" % admin_username)
        debit = create_posting("Transfer", "adjustments", ticker, quantity,
                "debit", "Adjustment (%s)" % admin_username)
        credit["count"] = 2
        debit["count"] = 2
        credit["uid"] = uid
        debit["uid"] = uid

        # The administrator should know if there is an error
        def postFailure(failure):
            log.err(failure)
            return failure

        return self.post_or_fail(credit, debit).addErrback(postFailure)

    def get_position_value(self, username, ticker):
        """Return the numeric value of a user's position for a contact. If it does not exist, return 0.

        :param username: the username
        :type username: str, models.User
        :param ticker: the contract
        :type ticker: str, models.User
        :returns: int -- the position value
        """
        user = self.get_user(username)
        contract = self.get_contract(ticker)
        try:
            return self.session.query(models.Position).filter_by(
                user=user, contract=contract).one().position
        except NoResultFound:
            return 0

    def get_margin(self, username):
        user = self.get_user(username)
        low_margin, high_margin, cash_spent = margin.calculate_margin(user, self.session, safe_prices=self.safe_prices)
        cash_position = self.get_position_value(username, 'BTC')
        return {
            'username': username,
            'low_margin': low_margin,
            'high_margin': high_margin,
            'cash_position': cash_position,
        }

    def get_position(self, username, ticker, reference_price=None):
        """Return a user's position for a contact. If it does not exist, initialize it. WARNING: If a position is created, it will be added to the session.

        :param username: the username
        :type username: str, models.User
        :param ticker: the contract
        :type ticker: str, models.User
        :param reference_price: the (optional) reference price for the position
        :type reference_price: int
        :returns: models.Position -- the position object
        """

        user = self.get_user(username)
        contract = self.get_contract(ticker)

        try:
            position = self.session.query(models.Position).filter_by(
                user=user, contract=contract).one()
            if position.reference_price is None and reference_price is not None:
                position.reference_price = reference_price
                self.session.add(position)
            return position
        except NoResultFound:
            log.msg("Creating new position for %s on %s." %
                          (username, contract))
            position = models.Position(user, contract)
            position.reference_price = reference_price
            self.session.add(position)
            return position

    def check_margin(self, user, low_margin, high_margin):
        cash = self.get_position_value(user, "BTC")

        log.msg("high_margin = %d, low_margin = %d, cash_position = %d" %
                     (high_margin, low_margin, cash))

        if high_margin > cash:
            return False
        else:
            return True

    def liquidation_value(self, position, quantity, book):
        sign = 1 if position.position < 0 else -1
        quantity *= sign

        position_override = {position.contract.ticker: { 'position': position.position + quantity,
                                                    'reference_price': position.reference_price,
                                                    'contract': position.contract } }


        if len(book['asks']):
            best_ask = book['asks'][0]['price']
        else:
            best_ask = sys.maxint

        if len(book['bids']):
            best_bid = book['bids'][0]['price']
        else:
            best_bid = 0

        if sign == 1:
            trade_price = best_ask
        else:
            trade_price = best_bid

        if position.contract.contract_type == "futures":
            cash_spent = util.get_cash_spent(position.contract, trade_price - position.reference_price, quantity)
        else:
            cash_spent = util.get_cash_spent(position.contract, trade_price, quantity)

        cash_position = self.get_position_value(position.username, position.contract.denominated_contract)
        cash_override = {position.contract.denominated_contract_ticker: cash_position - cash_spent}

        margin_current = margin.calculate_margin(position.user, self.session, self.safe_prices)
        margin_if = margin.calculate_margin(position.user, self.session, self.safe_prices,
                                                                      position_overrides=position_override,
                                                                      cash_overrides=cash_override)
        margin_change = margin_current[0] - margin_if[0]
        cost = (best_ask - best_bid)/2.0

        value = int(margin_change / cost)
        return value

    def liquidate_best(self, username):
        # Find the position that has the biggest margin impact and sell one of those
        user = self.get_user(username)

        # Cancel all open orders
        d = self.cancel_user_orders(user)

        def after_cancellations(results):
            log.msg("Cancels done for %s" % username)
            # Wait for pending postings
            total_pending = self.session.query(func.sum(models.Position.pending_postings).label("total_pending")).join(
                models.Contract).filter(
                models.Position.username==username).filter(
                models.Contract.contract_type.in_(["futures", "prediction"])).one().total_pending
            if total_pending > 0:
                d = task.deferLater(reactor, 300, after_cancellations, results)
                return d
            else:
                # Now figure out what order to place
                quantity = 1
                positions = self.session.query(models.Position).join(models.Contract).filter(models.Position.username==username).filter(
                    models.Contract.contract_type.in_(["futures", "prediction"])).filter(models.Position.position != 0)
                order_books = {}
                deferreds = []
                for position in positions:
                    if position.contract.ticker not in order_books:
                        d = self.engines[position.contract.ticker].get_order_book()
                        def got_book(book, ticker):
                            order_books[ticker] = book
                            return (ticker, book)

                        d.addCallback(got_book, position.contract.ticker)
                        deferreds.append(d)

                def got_all_books(all_books):
                    log.msg("Got all books: %s" % all_books)
                    liquidation_values = [(position, self.liquidation_value(position, quantity=quantity, book=order_books[position.contract.ticker])) for position in positions]
                    liquidation_values.sort(lambda x, y: y[1] - x[1])
                    log.msg("liquidation values: %s" % liquidation_values)

                    if len(liquidation_values):
                        return self.place_liquidation_order(liquidation_values[0][0], quantity=quantity)
                    else:
                        log.err("No positions to choose from!")
                        return None

                return defer.DeferredList(deferreds).addCallback(got_all_books)

        d.addCallback(after_cancellations)
        return d

    def liquidate_all(self, username):
        # Liquidate all positions for a user

        # Disable the user while this is happening
        self.disable_user(username)

        positions = self.session.query(models.Position).join(models.Contract).filter_by(username=username).filter(
            models.Contract.contract_type.in_(["futures", "prediction"]))
        deferreds = [self.liquidate_position(username, p.contract.ticker) for p in positions]
        dl = defer.DeferredList(deferreds)

        def reenable(results):
            self.enable_user(username)
            return results

        # Reenable the user once we are done
        dl.addCallback(reenable)
        return dl

    def place_liquidation_order(self, position, quantity=None):
            if position.position == 0:
                log.msg("Position is 0 not placing order")
                return None

            if position.position > 0:
                side = 'SELL'
                if quantity is None:
                    quantity = position.position

                price = 0
            else:
                side = 'BUY'
                if quantity is None:
                    quantity = -position.position

                # The maximum price
                if position.contract.contract_type == "prediction":
                    price = position.contract.denominator
                elif position.contract.contract_type == "futures":
                    price = sys.maxint
                else:
                    raise INVALID_CONTRACT_TYPE

            order = {
                'price': price,
                'quantity': quantity,
                'contract': position.contract.ticker,
                'side': side,
                'username': position.username,
                'timestamp': util.dt_to_timestamp(datetime.utcnow())
            }
            log.msg("Placing liquidation order: %s" % order)
            id = self.place_order(position.username, order, force=True)
            return id

    def liquidate_position(self, username, ticker):
        # Cancel all orders for a user, and liquidate his position with extreme prejudice

        # Cancel orders
        log.msg("liquidating %s for %s" % (ticker, username))
        user = self.get_user(username)
        contract = self.get_contract(ticker)
        orders = self.session.query(models.Order).filter_by(
            username=user.username).filter(
            models.Order.quantity_left>0).filter_by(
            is_cancelled=False).filter_by(
            contract=contract
        )
        log.msg("Cancelling orders for %s/%s" % (username, ticker))
        d = self.cancel_many_orders(orders)

        def after_cancellations(results):
            log.msg("Cancels for %s / %s done" % (username, ticker))
            # Wait until all pending postings have gone through
            try:
                position = self.session.query(models.Position).filter_by(user=user, contract=contract).one()
            except NoResultFound:
                # There is no position, return None
                return None

            if position.pending_postings > 0:
                d = task.deferLater(reactor, 300, after_cancellations, results)
            else:
                # Now place a closing out order
                return self.place_liquidation_order(position)


        d.addCallback(after_cancellations)
        return d

    def accept_order(self, order, force=False):
        """Accept the order if possible. Otherwise, delete the order

        :param order: Order object we wish to accept
        :type order: models.Order
        :raises: INSUFFICIENT_MARGIN, TRADE_NOT_PERMITTED
        """
        log.msg("Trying to accept order %s." % order)

        user = order.user

        if not force:
            # Audit the user
            if not self.is_user_enabled(user):
                log.msg("%s user is disabled" % user.username)
                try:
                    self.session.delete(order)
                    self.session.commit()
                except:
                    self.alerts_proxy.send_alert("Could not remove order: %s" % order)
                finally:
                    self.session.rollback()
                raise DISABLED_USER

            if not user.permissions.trade:
                log.msg("order %s not accepted because user %s not permitted to trade" % (order.id, user.username))
                try:
                    self.session.delete(order)
                    self.session.commit()
                except:
                    self.alerts_proxy.send_alert("Could not remove order: %s" % order)
                finally:
                    self.session.rollback()
                raise TRADE_NOT_PERMITTED

            low_margin, high_margin, max_cash_spent = margin.calculate_margin(
                order.user, self.session, self.safe_prices, order.id,
                trial_period=self.trial_period)

            if not self.check_margin(order.user, low_margin, high_margin):
                log.msg("Order rejected due to margin.")
                try:
                    self.session.delete(order)
                    self.session.commit()
                except:
                    self.alerts_proxy.send_alert("Could not remove order: %s" % order)
                finally:
                    self.session.rollback()
                raise INSUFFICIENT_MARGIN
        else:
            log.msg("Forcing order")

        log.msg("Order accepted.")
        order.accepted = True
        try:
            # self.session.merge(order)
            self.session.commit()
        except:
            self.alerts_proxy.send_alert("Could not merge order: %s" % order)
        finally:
            self.session.rollback()

    def charge_fees(self, fees, user, type="Trade"):
        """Credit fees to the people operating the exchange
        :param fees: The fees to charge ticker-index dict of fees to charge
        :type fees: dict
        :param username: the user to charge
        :type username: str, models.User

        """
        # TODO: Make this configurable
        import time

        # Make sure the vendorshares is less than or equal to 1.0
        assert(sum(self.vendor_share_config.values()) <= 1.0)
        user_postings = []
        vendor_postings = []
        remainder_postings = []
        last = time.time()
        user = self.get_user(user)

        for ticker, fee in fees.iteritems():
            contract = self.get_contract(ticker)

            # Debit the fee from the user's account
            user_posting = create_posting(type, user.username,
                    contract.ticker, fee, 'debit', note="Fee")
            user_postings.append(user_posting)

            remaining_fee = fee
            for vendor_name, vendor_share in self.vendor_share_config.iteritems():
                vendor_user = self.get_user(vendor_name)
                vendor_credit = int(fee * vendor_share)

                remaining_fee -= vendor_credit

                # Credit the fee to the vendor's account
                vendor_posting = create_posting(type,
                        vendor_user.username, contract.ticker, vendor_credit,
                        'credit', note="Vendor Credit")
                vendor_postings.append(vendor_posting)

            # There might be some fee leftover due to rounding,
            # we have an account for that guy
            # Once that balance gets large we distribute it manually to the
            # various share holders
            remainder_user = self.get_user('remainder')
            remainder_posting = create_posting(type,
                    remainder_user.username, contract.ticker, remaining_fee,
                    'credit')
            remainder_postings.append(remainder_posting)
            next = time.time()
            elapsed = (next - last) * 1000
            last = next
            log.msg("charge_fees: %s: %.3f ms." % (ticker, elapsed))

        return user_postings, vendor_postings, remainder_postings

    def post_transaction(self, username, transaction):
        """Update the database to reflect that the given trade happened. Charge fees.

        :param transaction: the transaction object
        :type transaction: dict
        """
        log.msg("Processing transaction %s." % transaction)
        last = time.time()
        if username != transaction["username"]:
            raise RemoteCallException("username does not match transaction")

        aggressive = transaction["aggressive"]
        ticker = transaction["contract"]
        order = transaction["order"]
        other_order = transaction["other_order"]
        side = transaction["side"]
        price = transaction["price"]
        quantity = transaction["quantity"]
        timestamp = transaction["timestamp"]
        uid = transaction["uid"]

        if ticker in self.clearing_contracts:
            raise CONTRACT_CLEARING

        contract = self.get_contract(ticker)

        if not contract.active:
            raise CONTRACT_NOT_ACTIVE

        user = self.get_user(username)

        next = time.time()
        elapsed = (next - last) * 1000
        last = next
        log.msg("post_transaction: part 1: %.3f ms." % elapsed)

        next = time.time()
        elapsed = (next - last) * 1000
        last = next
        log.msg("post_transaction: part 2: %.3f ms." % elapsed)

        if side == "BUY":
            denominated_direction = "debit"
            payout_direction = "credit"
        else:
            denominated_direction = "credit"
            payout_direction = "debit"

        if aggressive:
            ap = "Aggressive"
        else:
            ap = "Passive"

        note = "%s order: %s" % (ap, order)

        postings = []
        denominated_contract = contract.denominated_contract
        payout_contract = contract.payout_contract

        # Initialize the position here if it doesn't exist already
        if contract.contract_type == "futures":
            try:
                # We're not marking to market, we're keeping the same reference price
                # and making a cashflow based on the reference price
                denominated_contract = contract.denominated_contract
                payout_contract = contract
                position = self.get_position(user, contract, price)
                cash_spent = util.get_cash_spent(contract, price - position.reference_price, quantity)

                # Make sure the position goes into the db with this reference price
                self.session.add(position)
                self.session.commit()
            except Exception as e:
                self.session.rollback()
                log.err("Unable to add position %s to db" % position)
        else:
            cash_spent = util.get_cash_spent(contract, price, quantity)


        user_denominated = create_posting("Trade", username,
                denominated_contract.ticker, cash_spent, denominated_direction,
                note)
        user_payout = create_posting("Trade", username, payout_contract.ticker,
                quantity, payout_direction, note)
        postings.append(user_denominated)
        postings.append(user_payout)

        remote_postings = []
        if contract.contract_type == "futures":
            # Make the system posting for the cashflow because the other side might not have the same reference price
            # so his cashflow might be different, so we can't post directly against the counterparty
            system_posting = create_posting("Trade", "clearing_%s" % contract.ticker,
                                            denominated_contract.ticker, cash_spent, payout_direction,
                                            note)
            remote_postings.append(system_posting)

        # calculate fees
        fees = {}
        fees = util.get_fees(user, contract,
                price, quantity, trial_period=self.trial_period, ap="aggressive" if aggressive else "passive")


        user_fees, vendor_fees, remainder_fees = self.charge_fees(fees, user)

        next = time.time()
        elapsed = (next - last) * 1000
        log.msg("post_transaction: part 3: %.3f ms." % elapsed)

        postings.extend(user_fees)
        remote_postings.extend(vendor_fees)
        remote_postings.extend(remainder_fees)

        count = 2 * len(postings) + 2 * len(remote_postings)
        for posting in postings + remote_postings:
            posting["count"] = count
            posting["uid"] = uid

        for posting in remote_postings:
            self.accountant_proxy.remote_post(posting["username"], posting)

        if aggressive:
            try:
                aggressive_order = self.session.query(models.Order).filter_by(id=order).one()
                passive_order = self.session.query(models.Order).filter_by(id=other_order).one()

                trade = models.Trade(aggressive_order, passive_order, price, quantity)
                self.session.add(trade)
                self.session.commit()
                log.msg("Trade saved to db with posted=false: %s" % trade)
            except Exception as e:
                self.session.rollback()
                log.err("Exception while creating trade: %s" % e)

        d = self.post_or_fail(*postings)

        def update_order(result):
            try:
                db_order = self.session.query(models.Order).filter_by(id=order).one()
                db_order.quantity_left -= quantity
                # self.session.add(db_order)
                self.session.commit()
                log.msg("Updated order: %s" % db_order)
            except Exception as e:
                self.session.rollback()
                log.err("Unable to update order: %s" % e)

            self.webserver.order(username, db_order.to_webserver())
            log.msg("to ws: " + str({"order": [username, db_order.to_webserver()]}))
            return result

        def notify_fill(result):
            last = time.time()
            # Send notifications
            fill = {'contract': ticker,
                    'id': order,
                    'quantity': quantity,
                    'price': price,
                    'side': side,
                    'timestamp': timestamp,
                    'fees': fees
                   }
            self.webserver.fill(username, fill)
            log.msg('to ws: ' + str({"fills": [username, fill]}))

            next = time.time()
            elapsed = (next - last) * 1000
            log.msg("post_transaction: notify_fill: %.3f ms." % elapsed)

            # Now email the notification
            notifications = [n for n in user.notifications if n.type == "fill"]
            for notification in notifications:
                if notification.method == 'email':
                    t = util.get_locale_template(user.locale, self.jinja_env, 'fill.{locale}.email')
                    content = t.render(user=user, contract=contract, id=order, quantity=quantity, quantity_fmt=util.quantity_fmt(contract, quantity),
                                       price=price, price_fmt=util.price_fmt(contract, price), side=side, timestamp=util.timestamp_to_dt(timestamp)).encode('utf-8')

                    # Now email the token
                    log.msg("Sending mail: %s" % content)
                    s = self.sendmail.send_mail(content, to_address='<%s> %s' % (user.email,
                                                                                 user.nickname),
                                      subject='Order fill notification')

        def publish_trade(result):
            try:
                trade.posted = True
                # self.session.add(trade)
                self.session.commit()
                log.msg("Trade marked as posted: %s" % trade)
            except Exception as e:
                self.session.rollback()
                log.err("Exception when marking trade as posted %s" % e)

            self.webserver.trade(ticker, trade.to_webserver())
            log.msg("to ws: " + str({"trade": [ticker, trade.to_webserver()]}))
            return result


        # TODO: add errbacks for these
        d.addBoth(update_order)
        d.addCallback(notify_fill)
        if aggressive:
            d.addCallback(publish_trade)

        # The engine doesn't care to receive errors
        return d.addErrback(log.err)

    def raiseException(self, failure):
        raise failure.value

    def cancel_order(self, username, order_id):
        """Cancel an order by id.

        :param id: The order id to cancel
        :type id: int
        :returns: tuple -- (True/False, Result/Error)
        """
        log.msg("Received request to cancel order id %d." % order_id)

        try:
            order = self.session.query(models.Order).filter_by(id=order_id).one()
        except NoResultFound:
            raise NO_ORDER_FOUND

        if username is not None and order.username != username:
            raise USER_ORDER_MISMATCH

        if order.is_cancelled:
            raise ORDER_CANCELLED

        d = self.engines[order.contract.ticker].cancel_order(order_id)

        def update_order(result):
            try:
                order.is_cancelled = True
                # self.session.add(order)
                self.session.commit()
            except Exception as e:
                self.session.rollback()
                log.err("Unable to commit order cancellation")
                raise e

            return result

        def publish_order(result):
            self.webserver.order(username, order.to_webserver())
            return result

        d.addCallback(update_order)
        d.addCallback(publish_order)
        d.addErrback(self.raiseException)
        return d

    def cancel_order_engine(self, username, id):
        log.msg("Received msg from engine to cancel order id %d" % id)

        try:
            order = self.session.query(models.Order).filter_by(id=id).one()
        except NoResultFound:
            raise NO_ORDER_FOUND

        if username is not None and order.username != username:
            raise USER_ORDER_MISMATCH

        if order.is_cancelled:
            raise ORDER_CANCELLED

        # If the order has not been marked dispatched, it may have been in transit to the engine
        # when the engine bounced, and it may arrive at the engine and end up in the book,
        # after the engine told us to cancel it.
        # So tell the engine to cancel it, just in case the engine picked it up after reboot
        if not order.dispatched:
            self.engines[order.contract.ticker].cancel_order(order.id)

        try:
            order.is_cancelled = True
            # self.session.add(order)
            self.session.commit()
        except:
            self.alerts_proxy.send_alert("Could not merge cancelled order: %s" % order)
        finally:
            self.session.rollback()

        self.webserver.order(username, order.to_webserver())


    def place_order(self, username, order, force=False):
        """Place an order

        :param order: dictionary representing the order to be placed
        :type order: dict
        :returns: tuple -- (True/False, Result/Error)
        """
        if order["contract"] in self.clearing_contracts:
            raise CONTRACT_CLEARING

        user = self.get_user(order["username"])
        contract = self.get_contract(order["contract"])

        if not force:
            if not contract.active:
                raise CONTRACT_NOT_ACTIVE

            if contract.expired:
                raise CONTRACT_EXPIRED

            # do not allow orders for internally used contracts
            if contract.contract_type == 'cash':
                log.err("Webserver allowed a 'cash' contract!")
                raise INVALID_CONTRACT_TYPE

        if order["price"] % contract.tick_size != 0 or order["price"] < 0 or order["quantity"] < 0:
            raise INVALID_PRICE_QUANTITY

        # case of predictions
        if contract.contract_type == 'prediction':
            if not 0 <= order["price"] <= contract.denominator:
                raise INVALID_PRICE_QUANTITY

        if contract.contract_type == "cash_pair":
            if not order["quantity"] % contract.lot_size == 0:
                raise INVALID_PRICE_QUANTITY

        else:
            log.msg("Forcing order")

        o = models.Order(user, contract, order["quantity"], order["price"], order["side"].upper(),
                         timestamp=util.timestamp_to_dt(order['timestamp']))
        try:
            self.session.add(o)
            self.session.commit()
        except Exception as e:
            log.err("Error adding data %s" % e)
            self.session.rollback()
            raise e

        self.accept_order(o, force=force)
        d = self.engines[o.contract.ticker].place_order(o.to_matching_engine_order())

        def mark_order_dispatched(result):
            o.dispatched = True
            try:
                # self.session.add(o)
                self.session.commit()
            except:
                self.alerts_proxy.send_alert("Could not mark order as dispatched: %s" % o)
            finally:
                self.session.rollback()
            return result

        def publish_order(result):
            self.webserver.order(username, o.to_webserver())
            return result

        d.addErrback(self.raiseException)
        d.addCallback(mark_order_dispatched)
        d.addCallback(publish_order)

        return o.id

    def transfer_position(self, username, ticker, direction, quantity, note, uid):
        """Transfer a position from one user to another

        :param ticker: the contract
        :type ticker: str, models.Contract
        :param from_username: the user to transfer from
        :type from_username: str, models.User
        :param to_username: the user to transfer to
        :type to_username: str, models.User
        :param quantity: the qty to transfer
        :type quantity: int
        """
        posting = create_posting("Transfer", username, ticker, quantity,
                direction, note)
        posting['count'] = 2
        posting['uid'] = uid

        def transferFailure(failure):
            log.err(failure)
            return failure

        return self.post_or_fail(posting).addErrback(transferFailure)

    def request_withdrawal(self, username, ticker, amount, address):
        """See if we can withdraw, if so reduce from the position and create a withdrawal entry

        :param username:
        :param ticker:
        :param amount:
        :param address:
        :returns: bool
        :raises: INSUFFICIENT_MARGIN, WITHDRAW_NOT_PERMITTED
        """
        try:
            contract = self.get_contract(ticker)

            if self.trial_period:
                log.err("Withdrawals not permitted during trial period")
                raise WITHDRAW_NOT_PERMITTED

            log.msg("Withdrawal request for %s %s for %d to %s received" % (username, ticker, amount, address))
            user = self.get_user(username)
            if not user.permissions.withdraw:
                log.err("Withdraw request for %s failed due to no permissions" % username)
                raise WITHDRAW_NOT_PERMITTED

            if amount % contract.lot_size != 0:
                log.err("Withdraw request for a wrong lot_size qty: %d" % amount)
                raise INVALID_CURRENCY_QUANTITY


            # Audit the user
            if not self.is_user_enabled(user):
                log.err("%s user is disabled" % user.username)
                raise DISABLED_USER

            # Check margin now
            low_margin, high_margin, max_cash_spent = margin.calculate_margin(user,
                    self.session, self.safe_prices,
                    withdrawals={ticker:amount},
                    trial_period=self.trial_period)
            if not self.check_margin(username, low_margin, high_margin):
                log.msg("Insufficient margin for withdrawal %d / %d" % (low_margin, high_margin))
                raise INSUFFICIENT_MARGIN
            else:
                fees = util.get_withdraw_fees(user, contract, amount, trial_period=self.trial_period)

                amount -= fees[ticker]
                if amount < 0:
                    raise WITHDRAWAL_TOO_SMALL

                credit_posting = create_posting("Withdrawal",
                        'pendingwithdrawal', ticker, amount, 'credit', note=address)
                debit_posting = create_posting("Withdrawal", user.username,
                        ticker, amount, 'debit', note=address)
                my_postings = [credit_posting]
                remote_postings = [debit_posting]
                # Withdraw Fees
                user_postings, vendor_postings, remainder_postings = self.charge_fees(fees, user, type="Withdrawal")

                my_postings.extend(user_postings)
                remote_postings.extend(vendor_postings)
                remote_postings.extend(remainder_postings)

                count = len(remote_postings + my_postings)
                uid = util.get_uid()
                for posting in my_postings + remote_postings:
                    posting['count'] = count
                    posting['uid'] = uid

                d = self.post_or_fail(*my_postings)
                for posting in remote_postings:
                    self.accountant_proxy.remote_post(posting['username'], posting)

                def onSuccess(result):
                    self.cashier.request_withdrawal(username, ticker, address, amount)
                    return True

                def onError(failure):
                    log.err(failure)
                    return failure

                d.addCallback(onSuccess)
                d.addErrback(onError)

                return d
        except Exception as e:
            self.session.rollback()
            log.err("Exception received while attempting withdrawal: %s" % e)
            raise e

    def notify_deposit_overflow(self, user, contract, amount):
        """
        email notification of withdrawal pending to the user.
        """

        # Now email the notification
        t = util.get_locale_template(user.locale, self.jinja_env, 'deposit_overflow.{locale}.email')
        content = t.render(user=user, contract=contract, amount_fmt=util.quantity_fmt(contract, amount)).encode('utf-8')

        # Now email the token
        log.msg("Sending mail: %s" % content)
        s = self.sendmail.send_mail(content, to_address='<%s> %s' % (user.email,
                                                                     user.nickname),
                          subject='Your deposit was not fully processed')

    def deposit_cash(self, username, address, received, total=True, admin_username=None):
        """Deposits cash
        :param username: The username for this address
        :type username: str
        :param address: The address where the cash was deposited
        :type address: str
        :param received: how much total was received at that address
        :type received: int
        :param total: if True, then received is the total received on that address. If false, then received is just the most recent receipt
        :type total: bool
        """
        try:
            log.msg('received %d at %s - total=%s' % (received, address, total))

            #query for db objects we want to update

            total_deposited_at_address = self.session.query(models.Addresses).filter_by(address=address).one()
            contract = total_deposited_at_address.contract

            user_cash = self.get_position_value(total_deposited_at_address.username, contract.ticker)
            user = self.get_user(total_deposited_at_address.user)

            # compute deposit _before_ marking amount as accounted for
            if total:
                deposit = received - total_deposited_at_address.accounted_for
                total_deposited_at_address.accounted_for = received
            else:
                deposit = received
                total_deposited_at_address.accounted_for += deposit

            # update address
            # self.session.add(total_deposited_at_address)
            self.session.commit()

            #prepare cash deposit
            my_postings = []
            remote_postings = []
            if admin_username is not None:
                note = "%s (%s)" % (address, admin_username)
                cash_account = 'offlinecash'
            else:
                note = address
                cash_account = 'onlinecash'

            debit_posting = create_posting("Deposit", cash_account,
                                                  contract.ticker,
                                                  deposit,
                                                  'debit',
                                                  note=note)
            remote_postings.append(debit_posting)

            credit_posting = create_posting("Deposit", user.username,
                                                   contract.ticker,
                                                   deposit,
                                                   'credit',
                                                   note=note)
            my_postings.append(credit_posting)

            if total_deposited_at_address.contract.ticker in self.deposit_limits:
                deposit_limit = self.deposit_limits[total_deposited_at_address.contract.ticker]
            else:
                deposit_limit = float("inf")

            potential_new_position = user_cash + deposit
            excess_deposit = 0
            if not user.permissions.deposit:
                log.err("Deposit of %d failed for address=%s because user %s is not permitted to deposit" %
                              (deposit, address, user.username))

                # The user's not permitted to deposit at all. The excess deposit is the entire value
                excess_deposit = deposit
            elif potential_new_position > deposit_limit:
                log.err("Deposit of %d failed for address=%s because user %s exceeded deposit limit=%d" %
                              (deposit, address, total_deposited_at_address.username, deposit_limit))
                excess_deposit = potential_new_position - deposit_limit

            if excess_deposit > 0:
                if admin_username is not None:
                    note = "Excess Deposit: %s (%s)" % (address, admin_username)
                else:
                    note = "Excess Deposit: %s" % address
                # There was an excess deposit, transfer that amount into overflow cash
                excess_debit_posting = create_posting("Deposit",
                        user.username, contract.ticker, excess_deposit,
                        'debit', note=note)

                excess_credit_posting = create_posting("Deposit",
                        'depositoverflow', contract.ticker, excess_deposit,
                        'credit', note=note)

                my_postings.append(excess_debit_posting)
                remote_postings.append(excess_credit_posting)

                self.notify_deposit_overflow(user, contract, excess_deposit)

            # Deposit Fees
            fees = util.get_deposit_fees(user, contract, deposit, trial_period=self.trial_period)
            user_postings, vendor_postings, remainder_postings = self.charge_fees(fees, user, type="Deposit")

            my_postings.extend(user_postings)
            remote_postings.extend(vendor_postings)
            remote_postings.extend(remainder_postings)

            count = len(remote_postings + my_postings)
            uid = util.get_uid()
            for posting in my_postings + remote_postings:
                posting['count'] = count
                posting['uid'] = uid

            d = self.post_or_fail(*my_postings)
            for posting in remote_postings:
                self.accountant_proxy.remote_post(posting['username'], posting)

            def postingFailure(failure):
                log.err(failure)
                return failure

            return d.addErrback(postingFailure)
        except Exception as e:
            self.session.rollback()
            log.err(
                "Updating user position failed for address=%s and received=%d: %s" % (address, received, e))
            raise e

    def change_permission_group(self, username, id):
        """Changes a user's permission group to something different

        :param username: the user
        :type username: str, models.User
        :param id: the permission group id
        :type id: int
        """

        try:
            log.msg("Changing permission group for %s to %d" % (username, id))
            user = self.get_user(username)
            user.permission_group_id = id
            # self.session.add(user)
            self.session.commit()
            return None
        except Exception as e:
            log.err("Error: %s" % e)
            self.session.rollback()
            raise e
   
    def disable_user(self, user):
        user = self.get_user(user)
        log.msg("Disabling user: %s" % user.username)
        self.cancel_user_orders(user)
        self.disabled_users[user.username] = True

    def enable_user(self, user):
        user = self.get_user(user)
        log.msg("Enabling user: %s" % user.username)
        if user.username in self.disabled_users:
            del self.disabled_users[user.username]

    def is_user_enabled(self, user):
        user = self.get_user(user)
        if user.username in self.disabled_users:
            return False
        else:
            return True

    def cancel_user_orders(self, user):
        user = self.get_user(user)
        orders = self.session.query(models.Order).filter_by(
            username=user.username).filter(
            models.Order.quantity_left>0).filter_by(
            is_cancelled=False
        )
        return self.cancel_many_orders(orders)

    def cancel_many_orders(self, orders):
        deferreds = []
        for order in orders:
            log.msg("Cancelling user %s order %d" % (order.username, order.id))
            d = self.cancel_order(order.username, order.id)

            def cancel_failure(failure):
                log.err(failure)
                # Try again?
                log.msg("Trying again-- Cancelling user %s order %d" % (order.username, order.id))
                d = self.cancel_order(order.username, order.id)
                d.addErrback(cancel_failure)
                return d

            d.addErrback(cancel_failure)
            deferreds.append(d)

        return defer.DeferredList(deferreds)

    def get_my_users(self):
        users = self.session.query(models.User)
        my_users = []
        for user in users:
            if self.accountant_number == self.accountant_proxy.get_accountant_for_user(user.username):
                my_users.append(user)

        return my_users

    def repair_user_positions(self):
        my_users = self.get_my_users()
        for user in my_users:
            log.msg("Checking user %s" % user.username)
            for position in user.positions:
                if position.pending_postings > 0:
                    self.repair_user_position(user)
                    return

        log.msg("All users checked")

    def repair_user_position(self, user):
        user = self.get_user(user)
        log.msg("Repairing position for %s" % user.username)
        self.disable_user(user)
        try:
            for position in user.positions:
                position.pending_postings = 0
                # self.session.add(position)
            self.session.commit()
        except:
            self.session.rollback()
            self.alerts_proxy.send_alert("User %s in trouble. Cannot correct position!" % user.username)
            # Admin intervention required. ABORT!
            return

        reactor.callLater(300, self.check_user, user)

    def check_user(self, user):
        user = self.get_user(user)
        clean = True
        try:
            for position in user.positions:
                if position.pending_postings == 0:
                    # position has settled, sync with ledger
                    position.position, position.cp_timestamp = util.position_calculated(position, self.session)
                    position.position_checkpoint = position.position
                    # self.session.add(position)
                else:
                    clean = False
            if clean:
                log.msg("Correcting positions for user %s: %s" % (user.username, user.positions))
                self.session.commit()
                self.enable_user(user)
            else:
                # don't both committing, we are not ready yet anyway
                log.msg("User %s still not clean" % user.username)
                self.session.rollback()
                reactor.callLater(300, self.check_user, user)
        except:
            self.session.rollback()
            self.alerts_proxy.send_alert("User %s in trouble. Cannot correct position!" % user.username)
            # Admin intervention required. ABORT!
            return

    def clear_contract(self, ticker, price, uid):
        if ticker in self.clearing_contracts:
            raise CONTRACT_CLEARING

        contract = self.get_contract(ticker)

        if not contract.active:
            raise CONTRACT_NOT_ACTIVE

        if contract.expiration is None:
            raise NON_CLEARING_CONTRACT

        # For early clearing we don't pass in a price, we use safe_price
        if contract.expiration >= datetime.utcnow() and price is not None:
            raise CONTRACT_NOT_EXPIRED

        if contract.expiration < datetime.utcnow() and price is None:
            raise CONTRACT_EXPIRED

        # If there is no price, this is a mark-to-market
        # Clear to the safe price, and don't zero-out
        # the positions
        if price is None:
            price = self.safe_prices[ticker]
            zero_out = False
        else:
            zero_out = True

        # Mark contract as clearing
        log.msg("Marking %s as clearing" % ticker)
        self.clearing_contracts[ticker] = True

        my_users = [user.username for user in self.get_my_users()]

        # Cancel orders
        log.msg("Cancelling orders for %s" % ticker)
        orders = self.session.query(models.Order).filter_by(contract=contract).filter_by(is_cancelled=False).filter(
            models.Order.quantity_left > 0).filter(
            models.Order.username.in_(my_users))
        d = self.cancel_many_orders(orders)

        def after_cancellations(results):
            log.msg("Cancels done for %s" % ticker)
            # Wait until all pending postings have gone through
            total_pending = self.session.query(func.sum(models.Position.pending_postings).label('total_pending')).filter_by(contract=contract).filter(
                models.Position.username.in_(my_users)).one().total_pending
            if total_pending > 0:
                d = task.deferLater(reactor, 300, after_cancellations, results)
            else:
                all_positions = self.session.query(models.Position).filter_by(contract=contract)
                position_count = all_positions.count()
                my_positions = all_positions.filter(models.Position.username.in_(my_users))
                log.msg("clearing positions for %s" % ticker)
                results = [self.clear_position(position, price, position_count, uid, zero_out=zero_out) for position in my_positions]
                d = defer.DeferredList(results)
                def reactivate_contract(result):
                    log.msg("unmarking %s" % ticker)
                    del self.clearing_contracts[ticker]

                d.addCallback(reactivate_contract)

            return d

        d.addCallback(after_cancellations)
        return d

    def reload_fee_group(self, id):
        group = self.session.query(models.FeeGroup).filter_by(id=id).one()
        self.session.expire(group)

    def reload_contract(self, ticker):
        contract = self.session.query(models.Contract).filter_by(ticker=ticker).one()
        self.session.expire(contract)

    def change_fee_group(self, username, id):
        try:
            user = self.get_user(username)
            user.fee_group_id = id
            self.session.commit()
            return None
        except Exception as e:
            self.session.rollback()
            raise e

    def clear_position(self, position, price, position_count, uid, zero_out=True):
        # We use position_calculated here to be sure we get the canonical position
        position_calculated, timestamp = util.position_calculated(position, self.session)
        log.msg("Clearing position %s at %d" % (position, price))
        if position.contract.contract_type == "prediction":
            cash_spent = util.get_cash_spent(position.contract, price, position_calculated)
            note = "Clearing transaction for %s at price: %s" % (position.contract.ticker,
                                                                 util.price_fmt(position.contract, price))
            credit = create_posting("Clearing", position.username,
                    position.contract.denominated_contract.ticker, cash_spent, 'credit',
                    note)
            debit = create_posting("Clearing", position.username, position.contract.ticker,
                    position_calculated, 'debit', note)

            for posting in credit, debit:
                posting['count'] = position_count * 2
                posting['uid'] = uid
            log.msg("credit: %s, debit: %s" % (credit, debit))
        
            # TODO: Determine what the caller needs - do they want to know about errors?
            return self.post_or_fail(credit, debit).addErrback(log.err)

        elif position.contract.contract_type == "futures":
            if position.reference_price is None:
                log.err("Position %s has no reference price!")
                return defer.succeed(None)

            cash_spent = util.get_cash_spent(position.contract, price - position.reference_price, position_calculated)
            note = "Clearing transaction for %s at price: %s / reference_price: %s" % (position.contract.ticker,
                                                                                       util.price_fmt(position.contract, price),
                                                                                       util.price_fmt(position.contract, position.reference_price))
            credit = create_posting("Clearing", position.username,
                                    position.contract.denominated_contract_ticker, cash_spent, 'credit',
                                    note)
            clearing = create_posting("Clearing", "clearing_%s" % position.contract.ticker,
                                   position.contract.denominated_contract_ticker, cash_spent, 'debit',
                                   note)

            # We want a zero entry here to force a transaction for the contract
            # to be sent to the user so it knows to update reference_price
            zero = create_posting("Clearing", position.username,
                                  position.contract.ticker, 0, 'credit', note)

            # This is a simple two posting journal entry
            small_uid = util.get_uid()
            for posting in credit, clearing, zero:
                posting['count'] = 3
                posting['uid'] = small_uid
            log.msg("credit: %s, debit: %s, zero: %s" % (credit, clearing, zero))


            self.accountant_proxy.remote_post(clearing['username'], clearing)
            d = self.post_or_fail(credit, zero)

            def set_reference_price(result):
                log.msg("Setting reference price for %s to %d" % (position, price))
                try:
                    position.reference_price = price
                    self.session.commit()
                except Exception as e:
                    self.session.rollback()
                    raise e

                # Zero out positions
                if zero_out:
                    log.msg("Zeroing out position %s" % position)
                    debit = create_posting("Clearing", position.username,
                                           position.contract.payout_contract_ticker, position_calculated, 'debit',
                                           note)

                    # This one is big journal entry has to encompass everything
                    debit['count'] = position_count
                    debit['uid'] = uid
                    d = self.post_or_fail(debit)
                    return d
                else:
                    return result

            d.addCallback(set_reference_price).addErrback(log.err)
            return d
        else:
            raise INVALID_CONTRACT_TYPE


class WebserverExport(ComponentExport):
    """Accountant functions that are exposed to the webserver

    """
    def __init__(self, accountant):
        self.accountant = accountant
        ComponentExport.__init__(self, accountant)

    @export
    @session_aware
    @schema("rpc/accountant.webserver.json#place_order")
    def place_order(self, username, order):
        return self.accountant.place_order(username, order)

    @export
    @session_aware
    @schema("rpc/accountant.webserver.json#cancel_order")
    def cancel_order(self, username, id):
        return self.accountant.cancel_order(username, id)

    @export
    @session_aware
    @schema("rpc/accountant.webserver.json#request_withdrawal")
    def request_withdrawal(self, username, ticker, quantity, address):
        return self.accountant.request_withdrawal(username, ticker, quantity, address)

    @export
    @schema("rpc/accountant.webserver.json#get_margin")
    def get_margin(self, username):
        return self.accountant.get_margin(username)


class EngineExport(ComponentExport):
    """Accountant functions exposed to the Engine

    """
    def __init__(self, accountant):
        self.accountant = accountant
        ComponentExport.__init__(self, accountant)

    @export
    @session_aware
    def safe_prices(self, username, ticker, price):
        self.accountant.safe_prices[ticker] = price

    @export
    @session_aware
    @schema("rpc/accountant.engine.json#post_transaction")
    def post_transaction(self, username, transaction):
        return self.accountant.post_transaction(username, transaction)

    @export
    @session_aware
    @schema("rpc/accountant.engine.json#cancel_order")
    def cancel_order(self, username, id):
        return self.accountant.cancel_order_engine(username, id)


class CashierExport(ComponentExport):
    """Accountant functions exposed to the cashier

    """
    def __init__(self, accountant):
        self.accountant = accountant
        ComponentExport.__init__(self, accountant)

    @export
    @session_aware
    @schema("rpc/accountant.cashier.json#deposit_cash")
    def deposit_cash(self, username, address, received, total=True):
        return self.accountant.deposit_cash(username, address, received, total=total)

    @export
    @session_aware
    @schema("rpc/accountant.cashier.json#transfer_position")
    def transfer_position(self, username, ticker, direction, quantity, note, uid):
        return self.accountant.transfer_position(username, ticker, direction, quantity, note, uid)

    @export
    @session_aware
    @schema("rpc/accountant.cashier.json#get_position")
    def get_position(self, username, ticker):
        return self.accountant.get_position_value(username, ticker)

class AccountantExport(ComponentExport):
    """Accountant private chit chat link

    """
    def __init__(self, accountant):
        self.accountant = accountant
        ComponentExport.__init__(self, accountant)

    @export
    @session_aware
    @schema("rpc/accountant.accountant.json#remote_post")
    def remote_post(self, username, *postings):
        self.accountant.post_or_fail(*postings).addErrback(log.err)
        # we do not want or need this to propagate back to the caller
        return None

class RiskManagerExport(ComponentExport):
    def __init__(self, accountant):
        self.accountant = accountant
        ComponentExport.__init__(self, accountant)

    @export
    @schema("rpc/accountant.riskmanager.json#liquidate_best")
    def liquidate_best(self, username):
        return self.accountant.liquidate_best(username)

class AdministratorExport(ComponentExport):
    """Accountant functions exposed to the administrator

    """
    def __init__(self, accountant):
        self.accountant = accountant
        ComponentExport.__init__(self, accountant)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#adjust_position")
    def adjust_position(self, username, ticker, quantity, admin_username):
        return self.accountant.adjust_position(username, ticker, quantity, admin_username)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#transfer_position")
    def transfer_position(self, username, ticker, direction, quantity, note, uid):
        return self.accountant.transfer_position(username, ticker, direction, quantity, note, uid)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#change_permission_group")
    def change_permission_group(self, username, id):
        return self.accountant.change_permission_group(username, id)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#deposit_cash")
    def deposit_cash(self, username, address, received, total=True, admin_username=None):
        return self.accountant.deposit_cash(username, address, received, total=total, admin_username=admin_username)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#cancel_order")
    def cancel_order(self, username, id):
        return self.accountant.cancel_order(username, id)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#clear_contract")
    def clear_contract(self, username, ticker, price, uid):
        return self.accountant.clear_contract(ticker, price, uid)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#change_fee_group")
    def change_fee_group(self, username, id):
        return self.accountant.change_fee_group(username, id)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#reload_fee_group")
    def reload_fee_group(self, username, id):
        return self.accountant.reload_fee_group(id)

    @export
    @session_aware
    @schema("rpc/accountant.administrator.json#reload_contract")
    def reload_contract(self, username, ticker):
        return self.accountant.reload_contract(ticker)

    @export
    @schema("rpc/accountant.administrator.json#get_margin")
    def get_margin(self, username):
        return self.accountant.get_margin(username)

    @export
    @schema("rpc/accountant.administrator.json#liquidate_all")
    def liquidate_all(self, username):
        return self.accountant.liquidate_all(username)

    @export
    @schema("rpc/accountant.administrator.json#liquidate_position")
    def liquidate_position(self, username, ticker):
        return self.accountant.liquidate_position(username, ticker)

class AccountantProxy:
    def __init__(self, mode, uri, base_port, timeout=1):
        self.num_procs = config.getint("accountant", "num_procs")
        self.proxies = []
        for i in range(self.num_procs):
            if mode == "dealer":
                proxy = dealer_proxy_async(uri % (base_port + i), timeout=timeout)
            elif mode == "push":
                proxy = push_proxy_async(uri % (base_port + i))
            else:
                raise Exception("Unsupported proxy mode: %s." % mode)
            self.proxies.append(proxy)

    def get_accountant_for_user(self, username):
        return ord(username[0]) % self.num_procs

    def __getattr__(self, key):
        if key.startswith("__") and key.endswith("__"):
            raise AttributeError

        def routed_method(username, *args, **kwargs):
            if username is None:
                return [getattr(proxy, key)(None, *args, **kwargs) for proxy in self.proxies]
            else:
                proxy = self.proxies[self.get_accountant_for_user(username)]
                return getattr(proxy, key)(username, *args, **kwargs)

        return routed_method

if __name__ == "__main__":
    log.startLogging(sys.stdout)
    accountant_number = int(args[0])
    num_procs = config.getint("accountant", "num_procs")
    log.msg("Accountant %d of %d" % (accountant_number+1, num_procs))

    session = database.make_session()
    engines = {}
    engine_base_port = config.getint("engine", "accountant_base_port")
    for contract in session.query(models.Contract).filter_by(active=True).all():
        engines[contract.ticker] = dealer_proxy_async("tcp://127.0.0.1:%d" %
                                                      (engine_base_port + int(contract.id)))
    ledger = dealer_proxy_async(config.get("ledger", "accountant_export"), timeout=None)
    webserver = push_proxy_async(config.get("webserver", "accountant_export"))
    cashier = push_proxy_async(config.get("cashier", "accountant_export"))
    accountant_proxy = AccountantProxy("push",
            config.get("accountant", "accountant_export"),
            config.getint("accountant", "accountant_export_base_port"))
    alerts_proxy = AlertsProxy(config.get("alerts", "export"))
    debug = config.getboolean("accountant", "debug")
    trial_period = config.getboolean("accountant", "trial_period")
    mimetic_share = config.getfloat("accountant", "mimetic_share")
    sendmail = Sendmail(config.get("administrator", "email"))

    accountant = Accountant(session, engines, cashier, ledger, webserver, accountant_proxy, alerts_proxy,
                            accountant_number=accountant_number,
                            debug=debug,
                            trial_period=trial_period,
                            mimetic_share=mimetic_share,
                            sendmail=sendmail)

    webserver_export = WebserverExport(accountant)
    engine_export = EngineExport(accountant)
    cashier_export = CashierExport(accountant)
    administrator_export = AdministratorExport(accountant)
    accountant_export = AccountantExport(accountant)
    riskmanager_export = RiskManagerExport(accountant)

    watchdog(config.get("watchdog", "accountant") %
             (config.getint("watchdog", "accountant_base_port") + accountant_number))

    router_share_async(webserver_export,
                       config.get("accountant", "webserver_export") %
                       (config.getint("accountant", "webserver_export_base_port") + accountant_number))
    router_share_async(riskmanager_export,
                       config.get("accountant", "riskmanager_export") %
                       (config.getint("accountant", "riskmanager_export_base_port") + accountant_number))
    pull_share_async(engine_export,
                     config.get("accountant", "engine_export") %
                     (config.getint("accountant", "engine_export_base_port") + accountant_number))
    router_share_async(cashier_export,
                        config.get("accountant", "cashier_export") %
                        (config.getint("accountant", "cashier_export_base_port") + accountant_number))
    router_share_async(administrator_export,
                     config.get("accountant", "administrator_export") %
                     (config.getint("accountant", "administrator_export_base_port") + accountant_number))
    pull_share_async(accountant_export,
                       config.get("accountant", "accountant_export") %
                       (config.getint("accountant", "accountant_export_base_port") + accountant_number))

    reactor.callWhenRunning(accountant.repair_user_positions)
    reactor.run()

