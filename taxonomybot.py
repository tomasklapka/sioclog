#!/usr/bin/env python

"""taxonomybot.py - a helper IRC bot that relays user metadata via HTTP

Requires Twisted Python

Usage: 
taxonomybot.py server serverport nick user name localport logfile
For example:
taxonomybot.py irc.freenode.net 6667 taxbot sioc "Taxonomy bot" 1080 taxbot.log
"""

# ChangeLog:
# 2009-07-22: Initial version as a fork of sioclogbot.py

# TODO:
# * caching
# * more failsafe dialogue with nickserv
# * more complete www server?
# * expose more properties than just 'webid'
# * log splitting/rotating?
# * unit tests

import time
from traceback import print_exc
from urllib import unquote

from twisted.internet import protocol, reactor # you'll need python-twisted
from twisted.protocols import basic
from twisted.words.protocols import irc
from twisted.python.rebuild import rebuild

from twisted.web import http

import ircbase
ircbase.dbg = True
from ircbase import parseprefix, Line, Irc

import taxonomybot # XXX import myself for rebuild

def info(msg):
    print msg
err = info
dbg = True

def w3c_timestamp():
    t = time.localtime()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", t)
    if t.tm_isdst:
        timezone = time.altzone
    else:
        timezone = time.timezone
    return "%s%+03d:%02d" % (timestamp, -timezone/60.0/60, abs(timezone)/60%60)


class IrcServer(Irc):
    """Connection to the server."""
    def __init__(self):
        self.registered = False # whether the server has welcomed us
        self.nick = None # nick
        self.user = None # user@host
        self.serverprefix = None # irc.jyu.fi
        self.clientprefix = None # nick!user@host
        self.channels = [] # the channels we are currently on
        self.away = False # whether we are currently marked as being away
        self.awaymsg = None # the away message we have last requested

        self.timeoutCall = None # processed if server doesn't reply to PING

        # these are used to filter expected replies from the logs:
        self.whoreq = {} # pending WHO requests, channel -> client list
        self.whorep = {} # ongoing WHO responses, channel -> client list
        self.pingreq = {} # pending PING requests, token -> anything
        self.topicreq = {} # pending TOPIC requests, channel -> client list
        self.modereq = {} # pending MODE requests, channel -> client list
        self.namesreq = {} # pending NAMES requests, channel -> client list
        self.namesrep = {} # ongoing NAMES responses, channel -> client list

        # these are used to handle nickserv taxonomy replies:
        self.taxonomy_cbs = []
        self.taxonomy_state = None
        self.taxonomy_response = None

    def isme(self, prefix):
        return parseprefix(prefix)[0] == self.nick

    # basic IRC client functionality:
    def connectionMade(self):
        info("Server connected!")
        self.factory.instance = self
        self.sendLine(Line("NICK",[self.factory.nick]))
        self.sendLine(Line("USER",[self.factory.user, "*", "*",
                                   self.factory.name]))
        self.timeoutCall = reactor.callLater(2*60, self.pingTimeout) # XXX

    def connectionLost(self, reason):
        err("Disconnected from server: %s" % reason.value)
        # FIXME Don't log if already logged as a QUIT
        self.logLine(Line('ERROR', ['Closing Link: %s[%s] (%s)'
                                    % (self.nick,
                                       self.user, reason.value)]))
        # XXX what else? inform clients?
        self.factory.instance = None

    def logLine(self, line):
        self.factory.logLine(line)
    handleReceivedFallback = logLine


    def irc_ERR_NICKNAMEINUSE(self, line):
        self.sendLine(Line("NICK", [line.args[1]+'_']))

    def irc_ERR_UNAVAILRESOURCE(self, line):
        if line.args[1][0] is not '#':
            self.irc_ERR_NICKNAMEINUSE(line)

    def irc_RPL_WELCOME(self, line):
        self.registered = True
        self.serverprefix = line.prefix
        self.nick = line.args[0]
        _, self.user = parseprefix(line.args[-1].split(' ')[-1])
        self.clientprefix = "%s!%s" % (self.nick, self.user)

        if self.timeoutCall:
            self.timeoutCall.cancel()
            self.timeoutCall = None

        self.ping()

        # enable + or - prefix on each msg indicating 
        self.sendLine(Line("CAPAB", ["IDENTIFY-MSG"]))

        for c in self.factory.channels:
            self.sendLine(Line("JOIN", [c]))
            
        if dbg: info("store: %s" % self.factory.store)
        for line in self.factory.store:
            self.sendLine(line)
        self.factory.store = []

        return False

    def irc_PING(self, line):
        self.sendLine(Line("PONG",[line.args[0]]))
        return True # don't log

    def irc_PONG(self, line):
        if line.args[1] in self.pingreq:
            del self.pingreq[line.args[1]]

            if line.args[1] == 'KEEPALIVE' and self.timeoutCall:
                self.timeoutCall.cancel()
                self.timeoutCall = None
                reactor.callLater(5*60, self.ping)

            return True # don't log
        else: return False

    # response filtering:
    def filter_oneline(self, reqlist, line, argindex = 1):
        obj = line.args[argindex]
        reps = reqlist.pop(obj, 'all')
        if reps != 'all':
            for rep in reps:
                rep.sendLine(line)
            return True # filter from others
        else: return False
    def filter_dataline(self, reqlist, replist, line, argindex = 1):
        """Filters data lines of data-end-replies from those who didn't
        request them. After the request is sent, this may be called several
        times."""
        obj = line.args[argindex]
        reps = replist.get(obj, None) # who the reply is going to
        if reps == None: # if new reply:
            # turn requestors into recipients, or 'all' if nobody specifically
            reps = replist[obj] = reqlist.pop(obj, 'all')
        if reps != 'all':
            for rep in reps:
                rep.sendLine(line)
            return True # filter from others
        else: return False
    def filter_endline(self, reqlist, replist, line):
        """Filters end lines from data-end-replies from those who didn't
        request them. There might have been data lines before this, or not."""
        obj = line.args[1]
        reps = replist.pop(obj, None) # who the replies were to
        if reps == None: # if there were no data lines:
            reps = reqlist.pop(obj, 'all') # take requestors
        if reps != 'all':
            for rep in reps:
                rep.sendLine(line)
            return True # filter from others
        else: return False
    def irc_RPL_WHOREPLY(self, line):
        return self.filter_dataline(self.whoreq, self.whorep, line)
    def irc_RPL_ENDOFWHO(self, line):
        return self.filter_endline(self.whoreq, self.whorep, line)
    def irc_RPL_TOPIC(self, line):
        return self.filter_oneline(self.topicreq, line)
    irc_RPL_NOTOPIC = irc_RPL_TOPIC
    def irc_RPL_CHANNELMODEIS(self, line):
        return self.filter_oneline(self.modereq, line)
    def irc_RPL_UMODEIS(self, line):
        return self.filter_oneline(self.modereq, line, argindex=0)
    def irc_RPL_NAMREPLY(self, line):
        return self.filter_dataline(self.namesreq, self.namesrep, line,
                                    argindex=2)
    def irc_RPL_ENDOFNAMES(self, line):
        return self.filter_endline(self.namesreq, self.namesrep, line)

    # state tracking:
    def irc_NICK(self, line):
        # we get messages about other clients as well
        if self.isme(line.prefix):
            self.nick = line.args[0]
            self.clientprefix = self.nick + '!' + self.user
    def irc_JOIN(self, line):
        if self.isme(line.prefix):
            self.channels.append(line.args[0])
    def irc_PART(self, line):
        if self.isme(line.prefix):
            self.channels.remove(line.args[0])
    def irc_KICK(self, line):
        if line.args[1] == self.nick:
            self.channels.remove(line.args[0])
    def irc_RPL_UNAWAY(self, _):
        self.away = False
    def irc_RPL_NOWAWAY(self, _):
        self.away = True

    def addRequest(self, list, line):
        """Add the request in the line to the list."""
        obj = line.args[0]
        list[obj]=list.get(obj, []) + [line.source]
        # if request pending, don't send another: XXX timeout?
        if len(list[obj]) > 1: return True
        else: return False

    # actions:
    def sendLine(self, line):
        """send line to server, and add outbound requests to lists."""
        if line.cmd == 'PING':
            self.pingreq[line.args[0]] = True
        if line.cmd == 'WHO':
            self.addRequest(self.whoreq, line)
        if line.cmd == 'TOPIC' and len(line.args) == 1:
            self.addRequest(self.topicreq, line)
        if line.cmd == 'MODE' and len(line.args) == 1:
            self.addRequest(self.modereq, line)
        if line.cmd == 'NAMES':
            self.addRequest(self.namesreq, line)
        if line.cmd == 'AWAY':
            if len(line.args) > 0 and line.args[0] != '': # if setting away:
                self.awaymsg = line.args[0]

        if dbg: info("Sent to server: %s" % line)
        Irc.sendLine(self, line)

    def ping(self):
        self.sendLine(Line("PING", ["KEEPALIVE"]))
        self.timeoutCall = reactor.callLater(5*60, self.pingTimeout)

    def pingTimeout(self):
        self.timeoutCall = None
        self.loseConnection("Pong timeout")

    def loseConnection(self, reason="No reason"):
        self.sendLine(Line('QUIT', [reason]))
        self.logLine(Line('ERROR', ['Closing Link: %s[%s] (%s)'
                                    % (self.nick,
                                       self.user, reason)]))
        # XXX wait some time?
        Irc.loseConnection(self)

    def getTaxonomy(self, nick, response_cb):
        self.sendLine(Line('PRIVMSG', ['nickserv', 'taxonomy %s' % nick]))
        self.taxonomy_cbs.append(response_cb)

    def irc_NOTICE(self, line):
        if line.args[0] != self.nick:
            return False
        if parseprefix(line.prefix)[0] != "NickServ":
            return False
        msg = line.args[1][1:] # remove prefix + or -
        if msg.startswith("Taxonomy for \2"):
            nick = msg[len("Taxonomy for \2"):-2]
            self.taxonomy_state = nick
            self.taxonomy_response = []
        elif (msg.startswith("End of \2") or 
              msg.endswith("\2 is not registered.") or
             msg.startswith("Syntax: TAXONOMY ")):
            self.taxonomy_cbs[0](self.taxonomy_response)
            self.taxonomy_cbs.pop(0)
            self.taxonomy_state = self.taxonomy_response = None
        elif self.taxonomy_state:
            key, rest = msg.split(" ", 1)
            value = rest.split(":", 1)[1][1:]
            self.taxonomy_response.append((self.taxonomy_state, key, value))

    def irc_ERR_NOSUCHNICK(self, line):
        if line.args[1] != "nickserv":
            return False

        self.taxonomy_cbs[0](self.taxonomy_response)
        self.taxonomy_cbs.pop(0)
        self.taxonomy_state = self.taxonomy_response = None
        

class IrcServerFactory(protocol.ClientFactory):
    """Factory that will create an IrcServer object per connection."""
    protocol = IrcServer

    def __init__(self, server, serverport, nick, user, name, channels, logname):
        self.server = server
        self.serverport = serverport
        self.nick = nick
        self.user = user
        self.name = name
        self.channels = channels
        self.logname = logname

        self.store = [] # lines sent before we had a connection to the server
        self.backoff = None # fail fast if we can't make the first connection
        self.instance = None # the IrcServer object for the current connection

        self.logfile = file(self.logname, "ab")
        
    def clientConnectionLost(self, connector, reason):
        """If we get disconnected, reconnect to server."""
        err("Connection to server lost: %s" % reason.value)
        self.backoff = 5 # seconds. setting this starts retrying connects
        connector.connect()
        
    def clientConnectionFailed(self, connector, reason):
        """If connecting fails, schedule a retry."""
        err("Couldn't connect server: %s" % reason.value)
        if self.backoff != None:
            # XXX ReconnectingClientFactory implements jitter, we don't
            reactor.callLater(self.backoff, connector.connect)
            self.backoff = self.backoff * 2
        else:
            reactor.stop()

    def logLine(self, line):
        self.logfile.write("%s %s\r\n" % (w3c_timestamp(), line))
        self.logfile.flush()

class HttpRequest(http.Request):
    def __init__(self, ircserverfactory, *args, **kwargs):
        http.Request.__init__(self, *args, **kwargs)
        self.ircserverfactory = ircserverfactory

    def process(self):
        path = map(unquote, self.path[1:].split('/'))
        nick = path[0]
        self.ircserverfactory.instance.getTaxonomy(nick, self.taxonomy_cb)

    def taxonomy_cb(self, taxonomy):
        if taxonomy is None:
            taxonomy = []
        webids = [(s,p,o) for s,p,o in taxonomy
                  if p == 'webid']
        if webids:
            body = "<%s> <http://xmlns.com/foaf/0.1/holdsAccount> <irc://freenode/%s,isnick> .\n" % (webids[0][2], webids[0][0])
        else:
            body = ""
        self.setHeader('content-type', 'application/x-turtle')
        self.setHeader('content-length', str(len(body)))
        self.write(body)
        self.finish()

class HttpClientFactory(http.HTTPFactory):
    def __init__(self, ircserverfactory):
        http.HTTPFactory.__init__(self)
        self.ircserverfactory = ircserverfactory

    def buildProtocol(self, addr):
        p = protocol.ServerFactory.buildProtocol(self, addr)
        p.requestFactory = lambda *args, **kwargs: HttpRequest(self.ircserverfactory, *args, **kwargs)
        return p

# first when started, initiate a connection to the server
if __name__ == "__main__":
    import sys
    try:
        server = sys.argv[1]
        serverport = int(sys.argv[2])
        nick = sys.argv[3]
        user = sys.argv[4]
        name = sys.argv[5]
        channels = [] # sys.argv[6].split(",")
        localport = int(sys.argv[6])
        logfile = sys.argv[7]
    except Exception, e:
        err(str(e))
        err("Usage: %s server serverport nick user name localport logfile"
            % sys.argv[0])
        sys.exit(5)
    info("i am %s!%s :%s" % (nick, user, name))
    info("planning to join the channels %s" % repr(channels))
    info("connecting to %s on port %d..." % (server, serverport))
    ircserverfactory = taxonomybot.IrcServerFactory(server, serverport,
                                                   nick, user, name,
                                                   channels, logfile)
    reactor.connectTCP(server, serverport,
                       ircserverfactory)
    info("listening for http requests on port %d..." % localport)
    reactor.listenTCP(localport, taxonomybot.HttpClientFactory(ircserverfactory))
    info("entering main loop...")
    reactor.run()
    info("main loop done")
