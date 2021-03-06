#
# Copyright 2014 Mimetic Markets, Inc.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
from sputnik.plugin import Plugin
from autobahn.twisted.wamp import ApplicationSession
from twisted.internet.defer import inlineCallbacks, returnValue, maybeDeferred
from sputnik import observatory, rpc_schema

debug, log, warn, error, critical = observatory.get_loggers("plugin")

from autobahn import wamp
from autobahn.wamp.types import RegisterOptions

from jsonschema import ValidationError
from sputnik.exception import *

def error_handler(func):
    @inlineCallbacks
    def wrapped_f(*args, **kwargs):
        try:
            result = yield maybeDeferred(func, *args, **kwargs)
            returnValue({'success': True, 'result': result})
        except SputnikException as e:
            error("SputnikException received: %s" % str(e.args))
            returnValue({'success': False, 'error': e.args})
        except Exception as e:
            error("UNHANDLED EXCEPTION: %s" % str(e.args))
            error(e)
            returnValue({'success': False, 'error': ("exceptions/sputnik/generic_exception",)})

    return wrapped_f

def authenticated(func):
    def wrapper(*args, **kwargs):
        # Make sure username is not passed in
        if 'username' in kwargs:
            error("someone tried to pass 'username' in over RPC")
            raise WebserverException("exceptions/webserver/denied")

        details = kwargs.pop('details')
        # Authid can be a class attribute or a dict member
        try:
            username = getattr(details, 'authid')
        except AttributeError:
            username = details.get('authid')

        if username is None:
            raise Exception("details.authid is None")
        kwargs['username'] = username
        return maybeDeferred(func, *args, **kwargs)

    return wrapper


def schema(path, drop_args=["username"]):
    def wrap(f):
        func = inlineCallbacks(rpc_schema.schema(path, drop_args=drop_args)(f))
        def wrapped_f(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                return result
            except ValidationError as e:
                error(e)
                raise WebserverException("exceptions/webserver/schema_exception", str(f.validator.schema), e.args)
        return wrapped_f
    return wrap


class AuthenticationPlugin(Plugin):
    def onHello(self, router_session, realm, details):
        pass

    def onAuthenticate(self, router_session, signature, extra):
        pass

    def onJoin(self, router_session, details):
        pass

class AuthorizationPlugin(Plugin):
    def authorize(self, session, uri, action):
        pass

class SchemaPlugin(Plugin):
    def validate(self, router, type, uri, args, kwargs):
        pass

class ServicePlugin(Plugin, ApplicationSession):
    def __init__(self):
        ApplicationSession.__init__(self)
        Plugin.__init__(self)

    @inlineCallbacks
    def onJoin(self, details):
        results = yield self.register(self)
        for success, result in results:
            if success:
                log("Registered %s." % self._registrations[result.id].procedure)
            else:
                error("Error registering method: %s." % result.value.args[0])

class DatabasePlugin(Plugin):
    pass

class BackendPlugin(Plugin):
    pass

class ReceiverPlugin(Plugin):
    pass
