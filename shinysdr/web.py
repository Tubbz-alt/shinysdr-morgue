# Copyright 2013, 2014, 2015 Kevin Reid <kpreid@switchb.org>
# 
# This file is part of ShinySDR.
# 
# ShinySDR is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# ShinySDR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.  If not, see <http://www.gnu.org/licenses/>.

# pylint: disable=maybe-no-member, attribute-defined-outside-init, no-init, method-hidden, signature-differs
# (maybe-no-member is incorrect)
# (attribute-defined-outside-init is a Twisted convention for protocol objects)
# (no-init is pylint being confused by interfaces)
# (method-hidden: done on purpose)
# (signature-differs: twisted is inconsistent about connectionMade/connectionLost)


from __future__ import absolute_import, division

import json
import urllib
import os.path
import struct
import time
import weakref

from twisted.application import strports
from twisted.application.service import Service
from twisted.internet import defer
from twisted.internet import protocol
from twisted.internet import reactor as the_reactor  # TODO fix
from twisted.plugin import IPlugin, getPlugins
from twisted.python import log
from twisted.web import http, static, server, template
from twisted.web.resource import Resource
from zope.interface import Interface, implements, providedBy  # available via Twisted

from gnuradio import gr

import autobahn.twisted.websocket

import shinysdr.plugins
import shinysdr.db
from shinysdr.ephemeris import EphemerisResource
from shinysdr.modes import get_modes
from shinysdr.signals import SignalType
from shinysdr.values import ExportedState, BaseCell, BlockCell, StreamCell, IWritableCollection, the_poller


# used externally
staticResourcePath = os.path.join(os.path.dirname(__file__), 'webstatic')


_templatePath = os.path.join(os.path.dirname(__file__), 'webparts')


# Do not use this directly in general; use _serialize.
_json_encoder_for_serial = json.JSONEncoder(
    ensure_ascii=False,
    check_circular=False,
    allow_nan=True,
    sort_keys=True,
    separators=(',', ':'))


def _transform_for_json(obj):
    # Cannot implement this using the default hook in JSONEncoder because we want to override the behavior for namedtuples, which cannot be done otherwise.
    
    if isinstance(obj, SignalType):
        return {
            u'kind': obj.get_kind(),
            u'sample_rate': obj.get_sample_rate(),
        }
    elif isinstance(obj, tuple) and hasattr(obj, '_asdict'):  # namedtuple -- TODO better recognition?
        return {k: _transform_for_json(v) for k, v in obj._asdict().iteritems()}
    elif isinstance(obj, dict):
        return {k: _transform_for_json(v) for k, v in obj.iteritems()}
    elif isinstance(obj, (list, tuple)):
        return map(_transform_for_json, obj)
    else:
        return obj


# JSON-encode values for clients, including in the state stream
def _serialize(obj):
    structure = _transform_for_json(obj)
    return _json_encoder_for_serial.encode(structure)


class _SlashedResource(Resource):
    '''Redirects /.../this to /.../this/.'''
    
    def render(self, request):
        request.setHeader('Location', request.childLink(''))
        request.setResponseCode(http.MOVED_PERMANENTLY)
        return ''


class CellResource(Resource):
    isLeaf = True

    def __init__(self, cell, noteDirty):
        self._cell = cell
        # TODO: instead of needing this hook, main should use poller
        self._noteDirty = noteDirty

    def grparse(self, value):
        raise NotImplementedError()

    def grrender(self, value, request):
        return str(value)

    def render_GET(self, request):
        return self.grrender(self._cell.get(), request)

    def render_PUT(self, request):
        data = request.content.read()
        self._cell.set(self.grparse(data))
        request.setResponseCode(204)
        self._noteDirty()
        return ''
    
    def resourceDescription(self):
        return self._cell.description()


class ValueCellResource(CellResource):
    def __init__(self, cell, noteDirty):
        CellResource.__init__(self, cell, noteDirty)

    def grparse(self, value):
        return json.loads(value)

    def grrender(self, value, request):
        return _serialize(value).encode('utf-8')


def not_deletable():
    raise Exception('Attempt to delete session root')


class BlockResource(Resource):
    isLeaf = False

    def __init__(self, block, noteDirty, deleteSelf):
        Resource.__init__(self)
        self._block = block
        self._noteDirty = noteDirty
        self._deleteSelf = deleteSelf
        self._dynamic = block.state_is_dynamic()
        # Weak dict ensures that we don't hold references to blocks that are no longer held by this block
        self._blockResourceCache = weakref.WeakKeyDictionary()
        if not self._dynamic:  # currently dynamic blocks can only have block children
            self._blockCells = {}
            for key, cell in block.state().iteritems():
                if cell.isBlock():
                    self._blockCells[key] = cell
                else:
                    self.putChild(key, ValueCellResource(cell, self._noteDirty))
        self.__element = _BlockHtmlElement()
    
    def getChild(self, name, request):
        if self._dynamic:
            curstate = self._block.state()
            if name in curstate:
                cell = curstate[name]
                if cell.isBlock():
                    return self.__getBlockChild(name, cell.get())
        else:
            if name in self._blockCells:
                return self.__getBlockChild(name, self._blockCells[name].get())
        # old-style-class super call
        return Resource.getChild(self, name, request)
    
    def __getBlockChild(self, name, block):
        r = self._blockResourceCache.get(block)
        if r is None:
            r = self.__makeChildBlockResource(name, block)
            self._blockResourceCache[block] = r
        return r
    
    def __makeChildBlockResource(self, name, block):
        def deleter():
            if not IWritableCollection.providedBy(self._block):
                raise Exception('Block is not a writable collection')
            self._block.delete_child(name)
        return BlockResource(block, self._noteDirty, deleter)
    
    def render_GET(self, request):
        accept = request.getHeader('Accept')
        if accept is not None and 'application/json' in accept:  # TODO: Implement or obtain correct Accept interpretation
            request.setHeader('Content-Type', 'application/json')
            return _serialize(self.resourceDescription()).encode('utf-8')
        else:
            request.setHeader('Content-Type', 'text/html;charset=utf-8')
            return renderElement(request, self.__element)
    
    def render_POST(self, request):
        '''currently only meaningful to create children of CollectionResources'''
        block = self._block
        if not IWritableCollection.providedBy(block):
            raise Exception('Block is not a writable collection')
        assert request.getHeader('Content-Type') == 'application/json'
        reqjson = json.load(request.content)
        key = block.create_child(reqjson)  # note may fail
        self._noteDirty()
        url = request.prePathURL() + '/receivers/' + urllib.quote(key, safe='')
        request.setResponseCode(201)  # Created
        request.setHeader('Location', url)
        # TODO consider a more useful response
        return _serialize(url).encode('utf-8')
    
    def render_DELETE(self, request):
        self._deleteSelf()
        self._noteDirty()
        request.setResponseCode(204)  # No Content
        return ''
    
    def resourceDescription(self):
        return self._block.state_description()
    
    def isForBlock(self, block):
        return self._block is block


class _BlockHtmlElement(template.Element):
    '''
    Template element for HTML page for an arbitrary block.
    '''
    loader = template.XMLFile(os.path.join(_templatePath, 'block.template.xhtml'))

    @template.renderer
    def _block_url(self, request, tag):
        return tag('/' + '/'.join([urllib.quote(x, safe='') for x in request.prepath]))


class FlowgraphVizResource(Resource):
    isLeaf = True
    
    def __init__(self, reactor, block):
        self.__reactor = reactor
        self.__block = block
    
    def render_GET(self, request):
        request.setHeader('Content-Type', 'image/png')
        process = self.__reactor.spawnProcess(
            DotProcessProtocol(request),
            '/usr/bin/env',
            env=None,  # inherit environment
            args=['env', 'dot', '-Tpng'],
            childFDs={
                0: 'w',
                1: 'r',
                2: 2
            })
        process.pipes[0].write(self.__block.dot_graph())
        process.pipes[0].loseConnection()
        return server.NOT_DONE_YET


class DotProcessProtocol(protocol.ProcessProtocol):
    def __init__(self, request):
        self.__request = request
    
    def outReceived(self, data):
        self.__request.write(data)
    
    def outConnectionLost(self):
        self.__request.finish()


def _fqn(class_):
    # per http://stackoverflow.com/questions/2020014/get-fully-qualified-class-name-of-an-object-in-python
    return class_.__module__ + '.' + class_.__name__


def _get_interfaces(obj):
    return [_fqn(interface) for interface in providedBy(obj)]


class _StateStreamObjectRegistration(object):
    # TODO messy
    def __init__(self, ssi, poller, obj, serial, url, refcount):
        self.__ssi = ssi
        self.obj = obj
        self.serial = serial
        self.url = url
        self.has_previous_value = False
        self.previous_value = None
        self.value_is_references = False
        self.__dead = False
        if isinstance(obj, BaseCell):
            self.__obj_is_cell = True
            if isinstance(obj, StreamCell):  # TODO kludge
                self.__poller_registration = poller.subscribe(obj, self.__listen_binary_stream)
                self.send_now_if_needed = lambda: None
            else:
                self.__poller_registration = poller.subscribe(obj, self.__listen_cell)
                self.send_now_if_needed = self.__listen_cell
        else:
            self.__obj_is_cell = False
            self.__poller_registration = poller.subscribe_state(obj, self.__listen_state)
            self.send_now_if_needed = lambda: self.__listen_state(self.obj.state())
        self.__refcount = refcount
    
    def __str__(self):
        return self.url
    
    def set_previous(self, value, is_references):
        if is_references:
            for obj in value.itervalues():
                if obj not in self.__ssi._registered_objs:
                    raise Exception("shouldn't happen: previous value not registered", obj)
        self.has_previous_value = True
        self.previous_value = value
        self.value_is_references = is_references
    
    def send_initial_value(self):
        '''kludge to get initial state sent'''
    
    def send_now_if_needed(self):
        # should be overridden in instance
        raise Exception('This placeholder should never get called')
    
    def get_object_which_is_cell(self):
        if not self.__obj_is_cell:
            raise Exception('This object is not a cell')
        return self.obj
    
    def __listen_cell(self):
        if self.__dead:
            return
        obj = self.obj
        if isinstance(obj, StreamCell):
            raise Exception("shouldn't happen: StreamCell here")
        if obj.isBlock():
            block = obj.get()
            self.__ssi._lookup_or_register(block, self.url)
            self.__maybesend_reference({u'value': block}, True)
        else:
            value = obj.get()
            self.__maybesend(value, value)
    
    def __listen_binary_stream(self, value):
        if self.__dead:
            return
        self.__ssi._send1(True, struct.pack('I', self.serial) + value)
    
    def __listen_state(self, state):
        if self.__dead:
            return
        self.__maybesend_reference(state, False)
    
    # TODO fix private refs to ssi here
    def __maybesend(self, compare_value, update_value):
        if not self.has_previous_value or compare_value != self.previous_value[u'value']:
            self.set_previous({u'value': compare_value}, False)
            self.__ssi._send1(False, ('value', self.serial, update_value))
    
    def __maybesend_reference(self, objs, is_single):
        registrations = {
            k: self.__ssi._lookup_or_register(v, self.url + '/' + urllib.unquote(k))
            for k, v in objs.iteritems()
        }
        serials = {k: v.serial for k, v in registrations.iteritems()}
        if not self.has_previous_value or objs != self.previous_value:
            for reg in registrations.itervalues():
                reg.inc_refcount()
            if is_single:
                self.__ssi._send1(False, ('value', self.serial, serials[u'value']))
            else:
                self.__ssi._send1(False, ('value', self.serial, serials))
            if self.has_previous_value:
                refs = self.previous_value.values()
                refs.sort()  # ensure determinism
                for obj in refs:
                    if obj not in self.__ssi._registered_objs:
                        raise Exception("Shouldn't happen: previous value not registered", obj)
                    self.__ssi._registered_objs[obj].dec_refcount_and_maybe_notify()
            self.set_previous(objs, True)
    
    def drop(self):
        # TODO this should go away in refcount world
        if self.__poller_registration is not None:
            self.__poller_registration.unsubscribe()
    
    def inc_refcount(self):
        if self.__dead:
            raise Exception('incing dead reference')
        self.__refcount += 1
    
    def dec_refcount_and_maybe_notify(self):
        if self.__dead:
            raise Exception('decing dead reference')
        self.__refcount -= 1
        if self.__refcount == 0:
            self.__dead = True
            self.__ssi.do_delete(self)
            
            # capture refs to decrement
            if self.value_is_references:
                refs = self.previous_value.values()
                refs.sort()  # ensure determinism
            else:
                refs = []
            
            # drop previous value
            self.previous_value = None
            self.has_previous_value = False
            self.value_is_references = False
            
            # decrement refs
            for obj in refs:
                self.__ssi._registered_objs[obj].dec_refcount_and_maybe_notify()


# TODO: Better name for this category of object
class StateStreamInner(object):
    def __init__(self, send, root_object, root_url, noteDirty, poller=the_poller):
        self.__poller = poller
        self._send = send
        self.__root_object = root_object
        self._cell = BlockCell(self, '_root_object')
        self._lastSerial = 0
        root_registration = _StateStreamObjectRegistration(ssi=self, poller=self.__poller, obj=self._cell, serial=0, url=root_url, refcount=0)
        self._registered_objs = {self._cell: root_registration}
        self.__registered_serials = {root_registration.serial: root_registration}
        self._send_batch = []
        self.__batch_delay = None
        self.__root_url = root_url
        self.__noteDirty = noteDirty
        root_registration.send_now_if_needed()
    
    def connectionLost(self, reason):
        for obj in self._registered_objs.keys():
            self.__drop(obj)
    
    def dataReceived(self, data):
        # TODO: handle json parse failure or other failures meaningfully
        command = json.loads(data)
        op = command[0]
        if op == 'set':
            op, serial, value, message_id = command
            registration = self.__registered_serials[serial]
            cell = registration.get_object_which_is_cell()
            t0 = time.time()
            cell.set(value)
            registration.send_now_if_needed()
            self._send1(False, ['done', message_id])
            t1 = time.time()
            # TODO: Define self.__str__ or similar such that we can easily log which client is sending the command
            log.msg('set %s to %r (%1.2fs)' % (registration, value, t1 - t0))
            self.__noteDirty()  # TODO fix things so noteDirty is not needed
        else:
            log.msg('Unrecognized state stream op received: %r' % (command,))
            
    
    def get__root_object(self):
        '''Accessor for implementing self._cell.'''
        return self.__root_object
    
    def do_delete(self, reg):
        self._send1(False, ('delete', reg.serial))
        self.__drop(reg.obj)
    
    def __drop(self, obj):
        registration = self._registered_objs[obj]
        registration.drop()
        del self.__registered_serials[registration.serial]
        del self._registered_objs[obj]
    
    def _lookup_or_register(self, obj, url):
        if obj in self._registered_objs:
            return self._registered_objs[obj]
        else:
            self._lastSerial += 1
            serial = self._lastSerial
            registration = _StateStreamObjectRegistration(ssi=self, poller=self.__poller, obj=obj, serial=serial, url=url, refcount=0)
            self._registered_objs[obj] = registration
            self.__registered_serials[serial] = registration
            if isinstance(obj, BaseCell):
                self._send1(False, ('register_cell', serial, url, obj.description()))
                if isinstance(obj, StreamCell):  # TODO kludge
                    pass
                elif not obj.isBlock():  # TODO condition is a kludge due to block cell values being gook
                    registration.set_previous({u'value': obj.get()}, False)
            elif isinstance(obj, ExportedState):
                self._send1(False, ('register_block', serial, url, _get_interfaces(obj)))
            else:
                # TODO: not implemented on client (but shouldn't happen)
                self._send1(False, ('register', serial, url))
            registration.send_now_if_needed()
            return registration
    
    def _flush(self):  # exposed for testing
        self.__batch_delay = None
        if len(self._send_batch) > 0:
            # unicode() because JSONEncoder does not reliably return a unicode rather than str object
            self._send(unicode(_serialize(self._send_batch)))
            self._send_batch = []
    
    def _send1(self, binary, value):
        if binary:
            # preserve order by flushing stored non-binary msgs
            # TODO: Implement batching for binary messages.
            self._flush()
            self._send(value)
        else:
            # Messages are batched in order to increase client-side efficiency since each incoming WebSocket message is always a separate JS event.
            self._send_batch.append(value)
            # TODO: Parameterize with reactor so we can test properly
            if not (self.__batch_delay is not None and self.__batch_delay.active()):
                self.__batch_delay = the_reactor.callLater(0, self._flush)


class AudioStreamInner(object):
    def __init__(self, reactor, send, block, audio_rate):
        self._send = send
        self._queue = gr.msg_queue(limit=100)
        self.__running = [True]
        self._block = block
        self._block.add_audio_queue(self._queue, audio_rate)
        
        send(unicode(self._block.get_audio_queue_channels()))
        
        reactor.callInThread(_AudioStream_read_loop, reactor, self._queue, self.__deliver, self.__running)
    
    def dataReceived(self, data):
        pass
    
    def connectionLost(self, reason):
        self._block.remove_audio_queue(self._queue)
        self.__running[0] = False
        # Insert a dummy message to ensure the loop thread unblocks; otherwise it will sit around forever, including preventing process shutdown.
        self._queue.insert_tail(gr.message())
    
    def __deliver(self, data_string):
        self._send(data_string, safe_to_drop=True)


def _AudioStream_read_loop(reactor, queue, deliver, running):
    # RUNS IN A SEPARATE THREAD.
    while running[0]:
        buf = ''
        message = queue.delete_head()  # blocking call
        if message.length() > 0:  # avoid crash bug
            buf += message.to_string()
        # Collect more queue contents to batch data
        while not queue.empty_p():
            message = queue.delete_head()
            if message.length() > 0:  # avoid crash bug
                buf += message.to_string()
        reactor.callFromThread(deliver, buf)


def _lookup_block(block, path):
    for i, path_elem in enumerate(path):
        cell = block.state().get(path_elem)
        if cell is None:
            raise Exception('Not found: %r in %r' % (path[:i + 1], path))
        elif not cell.isBlock():
            raise Exception('Not a block: %r in %r' % (path[:i + 1], path))
        block = cell.get()
    return block


class OurStreamProtocol(autobahn.twisted.websocket.WebSocketServerProtocol):
    def __init__(self, caps, noteDirty):
        autobahn.twisted.websocket.WebSocketServerProtocol.__init__(self)
        self._caps = caps
        self._seenValues = {}
        self.inner = None
        self.__noteDirty = noteDirty
    
    def onConnect(self, request):
        """WebSocketServerProtocol implementation"""
        try:
            loc = request.path
            log.msg('Stream connection to ', loc)
            path = [urllib.unquote(x) for x in loc.split('/')]
            assert path[0] == ''
            path[0:1] = []
            if path[0] in self._caps:
                root_object = self._caps[path[0]]
                path[0:1] = []
            elif None in self._caps:
                root_object = self._caps[None]
            else:
                raise Exception('Unknown cap')  # TODO better error reporting
            if len(path) == 1 and path[0] == 'audio':
                rate = int(json.loads(request.params['rate'][0]))
                self.inner = AudioStreamInner(the_reactor, self.__send, root_object, rate)
            elif len(path) >= 1 and path[0] == 'radio':
                # note _lookup_block may throw. TODO: Better error reporting
                root_object = _lookup_block(root_object, path[1:])
                self.inner = StateStreamInner(self.__send, root_object, loc, self.__noteDirty)  # note reuse of loc as HTTP path; probably will regret this
            else:
                raise Exception('Unknown path: %r' % (path,))  # TODO check if cleans up properly in autobahn
        except Exception, e:
            log.err('in onConnect: %s' % e)
            raise
    
    def onMessage(self, payload, is_binary):
        """WebSocketServerProtocol implementation"""
        self.inner.dataReceived(payload)
    
    def connectionLost(self, reason):
        """twisted Protocol implementation"""
        super(OurStreamProtocol, self).connectionLost(reason)
        if self.inner is not None:
            self.inner.connectionLost(reason)
    
    def __send(self, message, safe_to_drop=False):
        if False and len(self.transport.transport.dataBuffer) > 1000000:
            # TODO: condition is horrible implementation-diving kludge
            # Don't accumulate indefinite buffer if we aren't successfully getting it onto the network.
            
            if safe_to_drop:
                log.err('Dropping data going to stream ' + self.transport.location)
            else:
                log.err('Dropping connection due to too much data on stream ' + self.transport.location)
                self.transport.close(reason='Too much data buffered')
        else:
            if isinstance(message, unicode):
                self.sendMessage(message.encode('utf-8'), isBinary=False)
            else:
                self.sendMessage(message, isBinary=True)


class OurStreamFactory(autobahn.twisted.websocket.WebSocketServerFactory):
    protocol = OurStreamProtocol
    
    def __init__(self, caps, noteDirty):
        autobahn.twisted.websocket.WebSocketServerFactory.__init__(self)
        self.__caps = caps
        self.__noteDirty = noteDirty
    
    def buildProtocol(self, addr):
        """twisted Factory implementation"""
        p = self.protocol(self.__caps, self.__noteDirty)
        p.factory = self
        return p


class IClientResourceDef(Interface):
    '''
    Client plugin interface object
    '''
    # Only needed to make the plugin system work
    # TODO write interface methods anyway


class ClientResourceDef(object):
    implements(IPlugin, IClientResourceDef)
    
    def __init__(self, key, resource, load_css_path=None, load_js_path=None):
        self.key = key
        self.resource = resource
        self.load_css_path = load_css_path
        self.load_js_path = load_js_path


def _make_static(filePath):
    r = static.File(filePath)
    r.contentTypes['.csv'] = 'text/csv'
    r.indexNames = ['index.html']
    r.ignoreExt('.html')
    return r


def _reify(parent, name):
    '''
    Construct an explicit twisted.web.static.File child identical to the implicit one so that non-filesystem children can be added to it.
    '''
    r = parent.createSimilarFile(parent.child(name).path)
    parent.putChild(name, r)
    return r


def _strport_to_url(desc, scheme='http', path='/', socket_port=0):
    '''Construct a URL from a twisted.application.strports string.'''
    # TODO: need to know canonical domain name, not localhost; can we extract from the ssl cert?
    # TODO: strports.parse is deprecated
    (method, args, _) = strports.parse(desc, None)
    if socket_port == 0:
        socket_port = args[0]
    if method == 'TCP':
        return scheme + '://localhost:' + str(socket_port) + path
    elif method == 'SSL':
        return scheme + 's://localhost:' + str(socket_port) + path
    else:
        # TODO better error return
        return '???'


class _RadioIndexHtmlElement(template.Element):
    loader = template.XMLFile(os.path.join(_templatePath, 'index.template.xhtml'))
    
    def __init__(self, title):
        self.__title = unicode(title)
    
    @template.renderer
    def title(self, request, tag):
        return tag(self.__title)


class _RadioIndexHtmlResource(Resource):
    isLeaf = True

    def __init__(self, title):
        self.__element = _RadioIndexHtmlElement(title)

    def render_GET(self, request):
        return renderElement(request, self.__element)


def renderElement(request, element):
    # per http://stackoverflow.com/questions/8160061/twisted-web-resource-resource-with-twisted-web-template-element-example
    # should be replaced with twisted.web.template.renderElement once we have Twisted >= 12.1.0 available in MacPorts.
    
    # TODO: Instead of this kludge (here because it would be a syntax error in the XHTML template}, serve XHTML and fix the client-side issues that pop up due to element-name capitalization.
    request.write('<!doctype html>')
    
    d = template.flatten(request, element, request.write)
    
    def done(ignored):
        request.finish()
        return ignored
    
    d.addBoth(done)
    return server.NOT_DONE_YET
    

class WebService(Service):
    # TODO: Too many parameters
    def __init__(self, reactor, root_object, note_dirty, read_only_dbs, writable_db, http_endpoint, ws_endpoint, root_cap, title, flowgraph_for_debug):
        self.__http_port = http_endpoint
        self.__ws_port = ws_endpoint
        
        # Roots of resource trees
        # - appRoot is everything stateful/authority-bearing
        # - serverRoot is the HTTP '/' and static resources are placed there
        serverRoot = _make_static(staticResourcePath)
        if root_cap is None:
            appRoot = serverRoot
            self.__visit_path = '/'
            ws_caps = {None: root_object}
        else:
            serverRoot = _make_static(staticResourcePath)
            appRoot = _SlashedResource()
            serverRoot.putChild(root_cap, appRoot)
            self.__visit_path = '/' + urllib.quote(root_cap, safe='') + '/'
            ws_caps = {root_cap: root_object}
        
        self.__ws_factory = OurStreamFactory(ws_caps)
        
        # UI entry point
        appRoot.putChild('', _RadioIndexHtmlResource(title))
        
        # Exported radio control objects
        appRoot.putChild('radio', BlockResource(root_object, note_dirty, not_deletable))
        
        # Frequency DB
        appRoot.putChild('dbs', shinysdr.db.DatabasesResource(read_only_dbs))
        appRoot.putChild('wdb', shinysdr.db.DatabaseResource(writable_db))
        
        # Debug graph
        appRoot.putChild('flow-graph', FlowgraphVizResource(reactor, flowgraph_for_debug))
        
        # Ephemeris
        appRoot.putChild('ephemeris', EphemerisResource())
        
        # Construct explicit resources for merge.
        test = _reify(serverRoot, 'test')
        jasmine = _reify(test, 'jasmine')
        for name in ['jasmine.css', 'jasmine.js', 'jasmine-html.js']:
            jasmine.putChild(name, static.File(os.path.join(
                    os.path.dirname(__file__), 'deps/jasmine/lib/jasmine-core/', name)))
        
        client = _reify(serverRoot, 'client')
        client.putChild('require.js', static.File(os.path.join(
            os.path.dirname(__file__), 'deps/require.js')))
        client.putChild('text.js', static.File(os.path.join(
            os.path.dirname(__file__), 'deps/text.js')))
        
        _add_plugin_resources(client)
        
        self.__site = server.Site(serverRoot)
        self.__ws_port_obj = None
        self.__http_port_obj = None
    
    def startService(self):
        Service.startService(self)
        if self.__ws_port_obj is not None:
            raise Exception('Already started')
        self.__ws_port_obj = strports.listen(self.__ws_port, self.__ws_factory)
        self.__http_port_obj = strports.listen(self.__http_port, self.__site)
    
    def stopService(self):
        Service.stopService(self)
        if self.__ws_port_obj is None:
            raise Exception('Not started, cannot stop')
        # TODO: Does Twisted already have something to bundle up a bunch of ports for shutdown?
        return defer.DeferredList([
            self.__http_port_obj.stopListening(),
            self.__ws_port_obj.stopListening()])
    
    def get_url(self):
        port_num = self.__http_port_obj.socket.getsockname()[1]  # TODO touching implementation, report need for a better way (web_port_obj.port is 0 if specified port is 0, not actual port)
    
        return _strport_to_url(self.__http_port, socket_port=port_num, path=self.__visit_path)

    def announce(self, open_client):
        '''interface used by shinysdr.main'''
        url = self.get_url()
        if open_client:
            log.msg('Opening ' + url)
            import webbrowser  # lazy load
            webbrowser.open(url, new=1, autoraise=True)
        else:
            log.msg('Visit ' + url)


def _add_plugin_resources(client_resource):
    # Plugin resources and plugin info
    load_list_css = []
    load_list_js = []
    mode_table = {}
    plugin_resources = Resource()
    client_resource.putChild('plugins', plugin_resources)
    for resource_def in getPlugins(IClientResourceDef, shinysdr.plugins):
        # Add the plugin's resource to static serving
        plugin_resources.putChild(resource_def.key, resource_def.resource)
        plugin_resource_url = '/client/plugins/' + urllib.quote(resource_def.key, safe='') + '/'
        # Tell the client to load the plugins
        # TODO constrain path values to be relative (not on a different origin, to not leak urls)
        if resource_def.load_css_path is not None:
            load_list_css.append(plugin_resource_url + resource_def.load_cs_path)
        if resource_def.load_js_path is not None:
            # TODO constrain value to be in the directory
            load_list_js.append(plugin_resource_url + resource_def.load_js_path)
    for mode_def in get_modes():
        mode_table[mode_def.mode] = {
            u'label': mode_def.label,
            u'can_transmit': mode_def.mod_class is not None
        }
    # Client gets info about plugins through this resource
    client_resource.putChild('plugin-index.json', static.Data(_serialize({
        u'css': load_list_css,
        u'js': load_list_js,
        u'modes': mode_table,
    }).encode('utf-8'), 'application/json'))
