
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

from twisted.protocols import dns, protocol
from twisted.internet import tcp, udp, main
from twisted.python import defer
import random, string, struct

DNS, TCP = range(2)

DNS_PORT = 53


class DNSBoss:
    protocols = (dns.DNS, dns.DNSOnTCP)
    portPackages = (udp, tcp)

    def __init__(self):
        self.pending = {}
        self.next = 0
        self.factories = [None, None]
        self.ports = [None, None]

    def __getstate__(self):
        dct = self.__dict__.copy()
        dct['pending'] = {}
        return dct

    def createFactory(self, i):
        if self.factories[i] is None:
            self.factories[i] = protocol.Factory()
            self.factories[i].protocol = self.protocols[i]
            self.factories[i].boss = self

    def createUDPFactory(self):
        self.createFactory(0)

    def createTCPFactory(self):
        self.createFactory(1)

    def createBothFactories(self):
        self.createFactory(0)
        self.createFactory(1)

    def startListening(self, i, portNum=0):
        self.createFactory(i)
        if self.ports[i] is None:
            self.ports[i] = self.portPackages[i].Port(portNum, 
                                                      self.factories[i])
            self.ports[i].startListening()

    def startListeningUDP(self, portNum=0):
        self.startListening(0, portNum)

    def startListeningTCP(self, portNum=0):
        self.startListening(1, portNum)

    def startListeningBoth(self, portNum = 0):
        self.startListening(0, portNum)
        self.startListening(1, portNum)

    def queryUDP(self, addr, name, callback, type=1, cls=1, recursive=1):
        self.startListeningUDP()
        transport = self.ports[0].createConnection(addr)
        return transport.protocol.query(name, callback, type, cls, recursive)

    def queryTCP(self, addr, name, callback, type=1, cls=1, recursive=1):
        self.createTCPFactory()
        protocol = self.factories[1].buildProtocol(addr)
        protocol.setQuery(name, callback, type, cls)
        transport = tcp.Client(addr[0], addr[1], protocol, recursive)

    def stopReading(self, i):
        if self.ports[i] is not None:
            self.ports[i].stopReading()
        self.ports[i], self.factories[i] = None, None

    def stopReadingUDP(self):
        self.stopReading(0)

    def stopReadingTCP(self):
        self.stopReading(1)

    def stopReadingBoth(self):
        self.stopReading(0)
        self.stopReading(1)

    def addPending(self, callback):
        self.next = self.next + 1
        self.pending[self.next] = callback
        return self.next

    def removePending(self, id):
        try:
            del self.pending[id]
        except KeyError:
            pass

    def accomplish(self, key, data):
        callback = self.pending.get(key)
        if callback is not None:
            del self.pending[key]
            callback(data)


class SentQuery:

    def __init__(self, name, type, callback, errback, boss, nameservers):
        self.callback = callback
        self.errback = errback
        self.ids = []
        self.done = 0
        self.boss = boss
        self.name = name
        self.type = type
        for nameserver in nameservers:
            self.ids.append(boss.queryUDP((nameserver, DNS_PORT), name, 
                                          self.getAnswer, type=type))

    def getAnswer(self, message):
        self.done = 1
        self.removeAll()
        if not message.answers:
            self.errback("No answers")
            return
        process = getattr(self, 'processAnswer_%d' % self.type, None)
        if process is None:
            self.errback("No processor for answer type %s" % self.type)
            return
        process(message)

    def processAnswer_1(self, message):
        '''looking for name->address resolution
        
        choose one of the IPs at random'''
        answers, cnames, cnameMap = [], [], {}
        for answer in message.answers:
            if answer.type in (1, 5):
                cnameMap[answer.name.name] = answer
            if answer.name.name != self.name:
                continue
            if answer.type == 1:
                answers.append(answer)
            elif answer.type == dns.CNAME:
                answer.strio.seek(answer.strioOff+2)
                n = dns.Name()
                n.decode(answer.strio)
                cnames.append(n.name)
        print cnames
        for name in cnames:
            if not cnameMap.has_key(name):
                continue
            for i in range(10):
                answer = cnameMap[name]
                if answer.type == 1:
                    answers.append(cnameMap[name])
                else:
                    answer.strio.seek(answer.strioOff+2)
                    n = dns.Name()
                    n.decode(answer.strio)
                    name = n.name
        if not answers:
            self.errback("No answers")
            return
        answer = random.choice(answers)
        self.callback(string.join(map(str, map(ord, answer.data)), '.'))

    def processAnswer_15(self, message):
        '''looking for Mail eXchanger for the domain

        order answers in in increasing priority, so the
        first MX is bad'''
        answers = []
        for answer in message.answers:
            priority = struct.unpack("!H", answer.data[:2])[0]
            answer.strio.seek(answer.strioOff+2)
            n = dns.Name()
            n.decode(answer.strio)
            answers.append((priority, n.name))
        answers.sort()
        ret = []
        for answer in answers:
            ret.append(answer[1])
        self.callback(ret)

    def timeOut(self):
        if not self.done:
            self.removeAll()
            self.errback("Timed out")
        self.done = 1

    def removeAll(self):
        for id in self.ids:
            self.boss.removePending(id)
        self.ids = []


class Resolver:

    def __init__(self, nameservers, boss=None):
        self.nameservers = nameservers
        self.boss = boss or DNSBoss()
        self.next = 0

    def resolve(self, name, type=1, timeout=10):
        """Run a DNS query, returning a Deferred for the result."""
        deferred = defer.Deferred()
        query = SentQuery(name, type, deferred.callback, deferred.errback, 
                          self.boss, self.nameservers)
        main.addTimeout(query.timeOut, timeout)
        return deferred


class ResolveConfResolver(Resolver):

    def __init__(self, file="/etc/resolv.conf", boss=None):
        Resolver.__init__(self, [], boss)
        self.file = file
        self._setNameServers()
       
    def __setstate__(self, dct):
        self.__dict__.update(dct)
        self._setNameServers()

    def _setNameServers(self):
        fp = open(self.file)
        lines = fp.readlines()
        fp.close()
        self.nameservers = []
        for line in map(string.split, lines):
            if line[0] == 'nameserver':
                self.nameservers.append(line[1])


class DNSServerMixin:

    def processQuery(self, message):
        self.factory.boss.getAnswers(self, message)


class DNS(DNSServerMixin, dns.DNS):
    pass


class DNSOnTCP(DNSServerMixin, dns.DNSOnTCP):
    pass

from cStringIO import StringIO

class MX(dns.RR):

    def encode(self, strio, compDict=None):
        self.name.encode(strio, compDict)
        s = StringIO()       
        s.write(struct.pack('!H', self.data[0]))
        dns.Name(self.data[1]).encode(s, compDict)
        strio.write(struct.pack(self.fmt, self.type, self.cls,
                                self.ttl, len(s.getvalue())))
        strio.write(s.getvalue())

class NS(dns.RR):
    def encode(self, strio, compDict=None):
        self.name.encode(strio, compDict)
        s = StringIO()       
        dns.Name(self.data).encode(s, compDict)
        strio.write(struct.pack(self.fmt, self.type, self.cls,
                                self.ttl, len(s.getvalue())))
        strio.write(s.getvalue())


class SimpleDomain:

    ttl = 60*60*24

    def __init__(self, name, ip):
        self.name = name
        self.ip = string.join(map(chr, map(int, string.split(ip, '.'))), '')

    def getAnswers(self, message, name, type):
        if type == dns.MX:
            message.answers.append(MX(name, ttl=self.ttl, type=dns.MX, 
                                      cls=dns.IN, data=(5, self.name)))
            message.add.append(dns.RR(self.name, ttl=self.ttl, type=dns.A,
                                      cls=dns.IN, data=self.ip))
        if type == dns.A:
            message.answers.append(dns.RR(name, ttl=self.ttl, type=dns.A, 
                                          cls=dns.IN, data=self.ip))
        message.ns.append(NS(self.name, ttl=self.ttl, type=dns.NS, cls=dns.IN, 
                                      data='ns.'+self.name))
        message.add.append(dns.RR('ns.'+self.name, ttl=self.ttl, type=dns.A, 
                                  cls=dns.IN, data=self.ip))
         


class DNSServerBoss(DNSBoss):

    protocols = (DNS, DNSOnTCP)

    def __init__(self):
        DNSBoss.__init__(self)
        self.domains = {}

    def addDomain(self, name, domain):
        self.domains[name] = domain

    def getAnswers(self, protocol, message):
        message.answer = 1
        message.rCode = dns.OK
        for query in message.queries:
            if query.cls!=dns.IN:
                continue # internet
            name = query.name.name
            while name and not self.domains.has_key(name):
                name = string.split(name, '.', 1)[1]
            if not name:
                continue
            self.domains[name].getAnswers(message, query.name.name, query.type)
        protocol.writeMessage(message)
        protocol.transport.loseConnection()
