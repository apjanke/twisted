# -*- Python -*-
# $Id: default.py,v 1.52 2002/11/25 22:03:48 exarkun Exp $
#
# Twisted, the Framework of Your Internet
# Copyright (C) 2001 Matthew W. Lefkowitz
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of version 2.1 of the GNU Lesser General Public
# License as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""Default reactor base classes, and a select() based reactor.

API Stability: stable

Maintainer: U{Itamar Shtull-Trauring<mailto:twisted@itamarst.org>}
"""

from bisect import insort
from time import time, sleep
import os
import socket
import sys

from twisted.internet.interfaces import IReactorCore, IReactorTime, IReactorUNIX
from twisted.internet.interfaces import IReactorTCP, IReactorUDP, IReactorSSL
from twisted.internet.interfaces import IReactorProcess, IReactorFDSet, IReactorMulticast
from twisted.internet import main, error, protocol, interfaces
from twisted.internet import tcp, udp, task, defer


from twisted.python import log, threadable, failure
from twisted.persisted import styles
from twisted.python.runtime import platform

from twisted.internet.base import ReactorBase

try:
    from twisted.internet import ssl
    sslEnabled = 1
except:
    sslEnabled = 0

from main import CONNECTION_LOST

if platform.getType() != 'java':
    import select
    from errno import EINTR, EBADF

if platform.getType() == 'posix':
    import process

if platform.getType() == "win32":
    try:
        import win32process
    except ImportError:
        win32process = None


class BaseConnector:
    """Basic implementation of connector.

    State can be: "connecting", "connected", "disconnected"
    """

    __implements__ = interfaces.IConnector

    timeoutID = None

    def __init__(self, reactor, factory, timeout):
        self.state = "disconnected"
        self.reactor = reactor
        self.factory = factory
        self.timeout = timeout

    def disconnect(self):
        """Disconnect whatever our are state is."""
        if self.state == 'connecting':
            self.stopConnecting()
        elif self.state == 'connected':
            self.transport.loseConnection()

    def connect(self):
        """Start connection to remote server."""
        if self.state != "disconnected":
            raise RuntimeError, "can't connect in this state"

        self.state = "connecting"
        self.factory.doStart()
        self.transport = transport = self._makeTransport()
        if self.timeout is not None:
            self.timeoutID = self.reactor.callLater(self.timeout, transport.failIfNotConnected, error.TimeoutError())
        self.factory.startedConnecting(self)

    def stopConnecting(self):
        """Stop attempting to connect."""
        if self.state != "connecting":
            raise RuntimeError, "we're not trying to connect"

        self.state = "disconnected"
        self.transport.failIfNotConnected(error.UserError())
        del self.transport

    def cancelTimeout(self):
        if self.timeoutID:
            try:
                self.timeoutID.cancel()
            except ValueError:
                pass
            del self.timeoutID

    def buildProtocol(self, addr):
        self.state = "connected"
        self.cancelTimeout()
        return self.factory.buildProtocol(addr)

    def connectionFailed(self, reason):
        self.cancelTimeout()
        self.state = "disconnected"
        self.factory.clientConnectionFailed(self, reason)
        if self.state == "disconnected":
            # factory hasn't called our connect() method
            self.factory.doStop()

    def connectionLost(self, reason):
        self.state = "disconnected"
        self.factory.clientConnectionLost(self, reason)
        if self.state == "disconnected":
            # factory hasn't called our connect() method
            self.factory.doStop()


class TCPConnector(BaseConnector):

    def __init__(self, reactor, host, port, factory, timeout, bindAddress):
        self.host = host
        self.port = port
        self.bindAddress = bindAddress
        BaseConnector.__init__(self, reactor, factory, timeout)

    def _makeTransport(self):
        return tcp.TCPClient(self.host, self.port, self.bindAddress, self, self.reactor)

    def getDestination(self):
        return ('INET', self.host, self.port)


class UNIXConnector(BaseConnector):

    def __init__(self, reactor, address, factory, timeout):
        self.address = address
        BaseConnector.__init__(self, reactor, factory, timeout)

    def _makeTransport(self):
        return tcp.UNIXClient(self.address, self, self.reactor)

    def getDestination(self):
        return ('UNIX', self.address)


class SSLConnector(BaseConnector):

    def __init__(self, reactor, host, port, factory, contextFactory, timeout, bindAddress):
        self.host = host
        self.port = port
        self.bindAddress = bindAddress
        self.contextFactory = contextFactory
        BaseConnector.__init__(self, reactor, factory, timeout)

    def _makeTransport(self):
        return ssl.Client(self.host, self.port, self.bindAddress, self.contextFactory, self, self.reactor)

    def getDestination(self):
        return ('SSL', self.host, self.port)





class PosixReactorBase(ReactorBase):
    """A basis for reactors that use file descriptors.
    """
    __implements__ = (ReactorBase.__implements__, IReactorUNIX,
                      IReactorTCP, IReactorUDP, IReactorMulticast) # IReactorProcess

    if sslEnabled:
        __implements__ = __implements__ + (IReactorSSL,)

    def __init__(self, installSignalHandlers=1):
        ReactorBase.__init__(self)
        self._installSignalHandlers = installSignalHandlers

    def _handleSignals(self):
        """Install the signal handlers for the Twisted event loop."""
        import signal
        signal.signal(signal.SIGINT, self.sigInt)
        signal.signal(signal.SIGTERM, self.sigTerm)

        # Catch Ctrl-Break in windows (only available in Python 2.2 and up)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.sigBreak)

        if platform.getType() == 'posix':
            signal.signal(signal.SIGCHLD, process.reapAllProcesses)

    def startRunning(self):
        threadable.registerAsIOThread()
        self.fireSystemEvent('startup')
        if self._installSignalHandlers:
            self._handleSignals()
        self.running = 1

    def run(self):
        self.startRunning()
        self.mainLoop()

    def mainLoop(self):
        while self.running:
            try:
                while self.running:
                    # Advance simulation time in delayed event
                    # processors.
                    self.runUntilCurrent()
                    t2 = self.timeout()
                    t = self.running and t2
                    self.doIteration(t)
            except:
                log.msg("Unexpected error in main loop.")
                log.deferr()
            else:
                log.msg('Main loop terminated.')


    def installWaker(self):
        """Install a `waker' to allow other threads to wake up the IO thread.
        """
        if not self.wakerInstalled:
            self.wakerInstalled = 1
            self.waker = _Waker()
            self.addReader(self.waker)


    # IReactorProcess

    def spawnProcess(self, processProtocol, executable, args=(), env={}, path=None,
                     uid=None, gid=None, usePTY = 0):
        p = platform.getType()
        if p == 'posix':
            if usePTY:
                return process.PTYProcess(self, executable, args, env, path, processProtocol, uid, gid)
            else:
                return process.Process(self, executable, args, env, path, processProtocol, uid, gid)
        # This is possible, just needs work - talk to itamar if you want this.
        #elif p == "win32":
        #    if win32process:
        #        threadable.init(1)
        #        import win32eventreactor
        #        return win32eventreactor.Process(self, processProtocol, executable, args, env, path)
        #    else:
        #        raise NotImplementedError, "process not available since win32all is not installed"
        else:
            raise NotImplementedError, "process only available in this reactor on POSIX"


    # IReactorUDP

    def listenUDP(self, port, protocol, interface='', maxPacketSize=8192):
        """Connects a given L{DatagramProtocol} to the given numeric UDP port.

        EXPERIMENTAL.

        @returns: object conforming to L{IListeningPort}.
        """
        p = udp.Port(self, port, protocol, interface, maxPacketSize)
        p.startListening()
        return p

    def connectUDP(self, remotehost, remoteport, protocol, localport=0,
                  interface='', maxPacketSize=8192):
        """Connects a L{ConnectedDatagramProtocol} instance to a UDP port.

        EXPERIMENTAL.
        """
        p = udp.ConnectedPort(self, (remotehost, remoteport), localport, protocol, interface, maxPacketSize)
        p.startListening()
        return p


    # IReactorMulticast

    def listenMulticast(self, port, protocol, interface='', maxPacketSize=8192):
        """Connects a given DatagramProtocol to the given numeric UDP port.

        EXPERIMENTAL.

        @returns: object conforming to IListeningPort.
        """
        p = udp.MulticastPort(self, port, protocol, interface, maxPacketSize)
        p.startListening()
        return p

    def connectMulticast(self, remotehost, remoteport, protocol, localport=0,
                         interface='', maxPacketSize=8192):
        """Connects a ConnectedDatagramProtocol instance to a UDP port.

        EXPERIMENTAL.
        """
        p = udp.ConnectedMulticastPort(self, (remotehost, remoteport), localport, protocol, interface, maxPacketSize)
        p.startListening()
        return p


    # IReactorUNIX

    def connectUNIX(self, address, factory, timeout=30):
        """@see twisted.internet.interfaces.IReactorUNIX.connectUNIX
        """
        c = UNIXConnector(self, address, factory, timeout)
        c.connect()
        return c

    def listenUNIX(self, address, factory, backlog=5):
        """Listen on a UNIX socket.
        """
        p = tcp.Port(address, factory, backlog=backlog)
        p.startListening()
        return p

    # IReactorTCP

    def listenTCP(self, port, factory, backlog=5, interface=''):
        """See twisted.internet.interfaces.IReactorTCP.listenTCP
        """
        p = tcp.Port(port, factory, backlog, interface)
        p.startListening()
        return p

    def connectTCP(self, host, port, factory, timeout=30, bindAddress=None):
        """See twisted.internet.interfaces.IReactorTCP.connectTCP
        """
        c = TCPConnector(self, host, port, factory, timeout, bindAddress)
        c.connect()
        return c

    # IReactorSSL (sometimes, not implemented)

    def connectSSL(self, host, port, factory, contextFactory, timeout=30, bindAddress=None):
        """See twisted.internet.interfaces.IReactorSSL.connectSSL
        """
        c = SSLConnector(self, host, port, factory, contextFactory, timeout, bindAddress)
        c.connect()
        return c

    def listenSSL(self, port, factory, contextFactory, backlog=5, interface=''):
        p = ssl.Port(port, factory, contextFactory, backlog, interface)
        p.startListening()
        return p



class _Win32Waker(log.Logger, styles.Ephemeral):
    """I am a workaround for the lack of pipes on win32.

    I am a pair of connected sockets which can wake up the main loop
    from another thread.
    """

    disconnected = 0

    def __init__(self):
        """Initialize.
        """
        log.msg("starting waker")
        # Following select_trigger (from asyncore)'s example;
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.setsockopt(socket.IPPROTO_TCP, 1, 1)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        client.connect(server.getsockname())
        reader, clientaddr = server.accept()
        client.setblocking(1)
        reader.setblocking(0)
        self.r = reader
        self.w = client
        self.fileno = self.r.fileno

    def wakeUp(self):
        """Send a byte to my connection.
        """
        self.w.send('x')

    def doRead(self):
        """Read some data from my connection.
        """
        self.r.recv(8192)

    def connectionLost(self, reason):
        self.r.close()
        self.w.close()


class _UnixWaker(log.Logger, styles.Ephemeral):
    """This class provides a simple interface to wake up the select() loop.

    This is necessary only in multi-threaded programs.
    """

    disconnected = 0

    def __init__(self):
        """Initialize.
        """
        i, o = os.pipe()
        self.i = os.fdopen(i,'r')
        self.o = os.fdopen(o,'w')
        self.fileno = self.i.fileno

    def doRead(self):
        """Read one byte from the pipe.
        """
        self.i.read(1)

    def wakeUp(self):
        """Write one byte to the pipe, and flush it.
        """
        self.o.write('x')
        self.o.flush()

    def connectionLost(self, reason):
        """Close both ends of my pipe.
        """
        self.i.close()
        self.o.close()

if platform.getType() == 'posix':
    _Waker = _UnixWaker
elif platform.getType() == 'win32':
    _Waker = _Win32Waker


# global state for selector
reads = {}
writes = {}


def win32select(r, w, e, timeout=None):
    """Win32 select wrapper."""
    if not r and not w:
        # windows select() exits immediately when no sockets
        if timeout == None:
            timeout = 0.1
        else:
            timeout = min(timeout, 0.001)
        sleep(timeout)
        return [], [], []
    r, w, e = select.select(r, w, w, timeout)
    return r, w+e, []

if platform.getType() == "win32":
    _select = win32select
else:
    _select = select.select


class SelectReactor(PosixReactorBase):
    """A select() based reactor - runs on all POSIX platforms and on Win32.
    """

    __implements__ = (PosixReactorBase.__implements__, IReactorFDSet)

    def _preenDescriptors(self):
        log.msg("Malformed file descriptor found.  Preening lists.")
        readers = reads.keys()
        writers = writes.keys()
        reads.clear()
        writes.clear()
        for selDict, selList in ((reads, readers), (writes, writers)):
            for selectable in selList:
                try:
                    select.select([selectable], [selectable], [selectable], 0)
                except:
                    log.msg("bad descriptor %s" % selectable)
                else:
                    selDict[selectable] = 1


    def doSelect(self, timeout,
                 # Since this loop should really be as fast as possible,
                 # I'm caching these global attributes so the interpreter
                 # will hit them in the local namespace.
                 reads=reads,
                 writes=writes,
                 rhk=reads.has_key,
                 whk=writes.has_key):
        """Run one iteration of the I/O monitor loop.

        This will run all selectables who had input or output readiness
        waiting for them.
        """
        while 1:
            try:
                r, w, ignored = _select(reads.keys(),
                                        writes.keys(),
                                        [], timeout)
                break
            except ValueError, ve:
                # Possibly a file descriptor has gone negative?
                self._preenDescriptors()
            except TypeError, te:
                # Something *totally* invalid (object w/o fileno, non-integral result)
                # was passed
                self._preenDescriptors()
            except select.error,se:
                # select(2) encountered an error
                if se.args[0] in (0, 2):
                    # windows does this if it got an empty list
                    if (not reads) and (not writes):
                        return
                    else:
                        raise
                elif se.args[0] == EINTR:
                    return
                elif se.args[0] == EBADF:
                    self._preenDescriptors()
                else:
                    # OK, I really don't know what's going on.  Blow up.
                    raise
        for selectables, method, dict in ((r, "doRead", reads),
                                          (w,"doWrite", writes)):
            hkm = dict.has_key
            for selectable in selectables:
                # if this was disconnected in another thread, kill it.
                if not hkm(selectable):
                    continue
                # This for pausing input when we're not ready for more.
                log.logOwner.own(selectable)
                try:
                    why = getattr(selectable, method)()
                    handfn = getattr(selectable, 'fileno', None)
                    if not handfn:
                        why = error.ConnectionFdescWentAway('Handler has no fileno method')
                    elif handfn() == -1:
                        why = error.ConnectionFdescWentAway('Filedescriptor went away')
                except:
                    log.deferr()
                    why = sys.exc_value
                if why:
                    self.removeReader(selectable)
                    self.removeWriter(selectable)
                    try:
                        selectable.connectionLost(failure.Failure(why))
                    except:
                        log.deferr()
                log.logOwner.disown(selectable)

    doIteration = doSelect

    def addReader(self, reader):
        """Add a FileDescriptor for notification of data available to read.
        """
        reads[reader] = 1

    def addWriter(self, writer):
        """Add a FileDescriptor for notification of data available to write.
        """
        writes[writer] = 1

    def removeReader(self, reader):
        """Remove a Selectable for notification of data available to read.
        """
        if reads.has_key(reader):
            del reads[reader]

    def removeWriter(self, writer):
        """Remove a Selectable for notification of data available to write.
        """
        if writes.has_key(writer):
            del writes[writer]

    def removeAll(self):
        """Remove all readers and writers, and return list of Selectables."""
        readers = reads.keys()
        for reader in readers:
            if reads.has_key(reader):
                del reads[reader]
            if writes.has_key(reader):
                del writes[reader]
        return readers


def install():
    """Configure the twisted mainloop to be run using the select() reactor.
    """
    reactor = SelectReactor(1)
    main.installReactor(reactor)


__all__ = ["install", "PosixReactorBase", "SelectReactor"]
