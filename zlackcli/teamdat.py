from collections import OrderedDict
import tempfile
import os.path
import urllib.parse
import subprocess
import aiohttp
import aiohttp.web
import asyncio

from .parsematch import ParseMatch, NeverMatch

"""
Virtual base classes, to be subclassed for each protcol.
"""

class Protocol:
    """Represents one protocol. Each implemented protocol (Slack, etc)
    is a subclass which is instantiated in the client.protocols array.
    """
    key = None
    hostclass = None   # A subclass of Host

    def __init__(self, client):
        self.client = client
        self.teams = OrderedDict()   # team.key to Host

    def __repr__(self):
        return '<%s (%s)>' % (self.__class__.__name__, self.key,)

    def create_team(self, map):
        """Create a team and add it to the team list(s). The argument
        is an OrderedDict of information.
        """
        # Call the HostClass's constructor.
        cla = self.hostclass
        team = cla(self, map)
        
        # Add it to both our team list and the master team list.
        self.teams[team.key] = team
        self.client.teams[team.key] = team
        
        return team

    async def open(self):
        """Open web sessions for the client, and one for each team,
        and then load the team data. (This does not open the websockets.)
        """
        raise NotImplementedError('open')
    
    async def close(self):
        """Shut down all our open sessions and whatnot, in preparation
        for quitting.
        """
        raise NotImplementedError('close')
    
    def begin_auth(self):
        """Launch the process of authenticating to a new Slack team.
        (This returns immediately.)
        """
        raise NotImplementedError('begin_auth')
    
    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) You typically won't want to customize this;
        instead, replace the Client.print() method.
        """
        self.client.print(msg)

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        self.client.print_exception(ex, '%s (%s)' % (label, self.key))

    def construct_auth_handler(self, future, statecheck):
        """Construct a handler for aiohttp.web.Server.
        This handler accepts the web request on port 8090. When a valid
        request is received, it sets a value in the passed-in future.
        (This is generic to all OAuth implementation, so it lives in
        Protocol.)
        """
        
        async def handler(request):
            map = request.query
            message = '???'
    
            if 'code' not in map:
                message = 'No code found.'
            elif 'state' not in map:
                message = 'No state field found.'
            elif map['state'] != statecheck:
                message = 'State field did not match.'
            else:
                code = map['code']
                future.set_result(code)
                message = 'Auth code received: %s\n' % (code,)
            
            return aiohttp.web.Response(text=message)
        
        return handler

class ProtoUI:
    """This module translates between the UI (human-readable input and
    output) and the protocol (with its protocol-specific messages).
    """
    # Protocol-specific slash commands
    handler_list = []

    def __init__(self, protocol):
        self.protocol = protocol
        self.client = protocol.client
        self.ui = protocol.client.ui

    def send_message(self, text, team, chanid):
        """Send a message to the given team and channel.
        (This returns immediately.)
        """
        raise NotImplementedError('send_message')
        
    async def fetch_data(self, team, dat):
        """Fetch data stored by note_file_data().
        """
        raise NotImplementedError('fetch_data')

    async def fetch_url(self, team, url):
        """Fetch the given URL, using the team's web credentials.
        Store the data in a temporary file.
        """
        self.print('[%s]: Fetching %s...' % (team.short_name(), url,))
        tup = urllib.parse.urlparse(url)
        async with team.session.get(url, max_redirects=4) as resp:
            dat = await resp.read()
            if resp.status != 200:
                self.print('Got HTTP error %s' % (resp.status,))
                return
            filename = os.path.basename(tup.path)
            pathname = os.path.join(tempfile.gettempdir(), filename)
            fl = open(pathname, 'wb')
            fl.write(dat)
            fl.close()
            self.print('Fetched %d bytes: %s' % (len(dat), pathname,))
            await self.display_path(pathname)
            
    async def display_path(self, pathname):
        """If the "viewfile" preference is set, run it to display
        the file at the given path.
        ### only do this on known MIME types?
        """
        opencmd = self.client.prefs.get('viewfile', None)
        if opencmd:
            args = opencmd.split(' ')
            args.append(pathname)
            proc = subprocess.Popen(args)
            # We have to wait and collect the process termination result.
            # (Quite possibly the process forked and exited immediately,
            # but not necessarily.)
            while proc.poll() is None:
                await asyncio.sleep(1)
        
    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) You typically won't want to customize this;
        instead, replace the Client.print() method.
        """
        self.client.print(msg)

class Host:
    """Represents one server, team, workspace, or what have you. The
    user logs into a host with a password or OAuth token.
    """
    
    protocol = None
    protocolkey = None
    
    # self.id: identifier, unique within protocol
    # self.key: "protocol:id"
    # self.users: map
    # self.channels: map
    # self.origmap: the OrderedDict that was used to construct the Host
    
    # self.nameparser: ParseMatch for the id and aliases

    def __repr__(self):
        return '<%s %s:%s "%s">' % (self.__class__.__name__, self.protocolkey, self.id, self.team_name)

    def name_parser(self):
        """Return a matcher for this host's name.
        """
        return NeverMatch()

    async def open(self):
        """Create the web API session, load the team data, and open
        the RTM socket (if desired).
        """
        raise NotImplementedError('open')
    
    async def close(self):
        """Shut down our session (and socket) for good.
        """
        raise NotImplementedError('close')
    
    def rtm_connect(self):
        """Open the RTM (real-time) websocket. If it's already connected,
        disconnect and reconnect.
        (Fire-and-forget call.)
        """
        task = self.evloop.create_task(self.rtm_connect_async())
        def callback(future):
            self.print_exception(future.exception(), 'RTM connect')
        task.add_done_callback(callback)
        
    def rtm_disconnect(self):
        """Close the RTM (real-time) websocket.
        (Fire-and-forget call.)
        """
        task = self.evloop.create_task(self.rtm_disconnect_async())
        def callback(future):
            self.print_exception(future.exception(), 'RTM disconnect')
        task.add_done_callback(callback)
        
    async def rtm_connect_async(self, from_reconnect=False):
        """Open the RTM (real-time) websocket. If it's already connected,
        disconnect and reconnect.
        """
        raise NotImplementedError('rtm_connect_async')
        
    async def rtm_disconnect_async(self, from_reconnect=False):
        """Close the RTM (real-time) websocket.
        """
        raise NotImplementedError('rtm_disconnect_async')
        
    def rtm_connected(self):
        """Check whether the RTM websocket is open.
        """
        raise NotImplementedError('rtm_connected')
    
    async def load_connection_data(self):
        """Load all the information we need for a connection: the channel
        and user lists.
        """
        raise NotImplementedError('load_connection_data')

    def get_aliases(self):
        """Return a list of team aliases or None.
        """
        ls = self.client.prefs.team_get('aliases', self)
        if ls:
            return ls
        return None

    def set_aliases(self, aliases):
        """Set a list of team aliases.
        """
        self.client.prefs.team_put('aliases', aliases, self)
        self.update_name_parser()

    def update_name_parser(self):
        """Update the matcher for this host's name, accepting current
        aliases.
        """
        self.nameparser = ParseMatch(self.team_name, self.client.prefs.team_get('aliases', self))
        
    def short_name(self):
        """Return the team name or the first alias.
        """
        ls = self.client.prefs.team_get('aliases', self)
        if ls:
            return ls[0]
        return self.team_name

    def set_last_channel(self, chanid):
        """Note the last channel used for this team.
        """
        pass
        
    def get_last_channel(self):
        """Get the last channel used for this team.
        """
        return None
        
    async def recap_channel(self, chanid, interval):
        """Recap the last interval seconds of a channel.
        """
        raise NotImplementedError('recap_channel')
        
    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) You typically won't want to customize this;
        instead, replace the Client.print() method.
        """
        self.client.print(msg)

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        self.client.print_exception(ex, '%s (%s)' % (label, self.short_name()))

    
class Channel:
    """Represents a discussion channel on a Host.
    """
    # self.team
    # self.id
    # self.name
    # self.client: same as self.team.client
    # self.private: is this not public to all users?
    # self.imuser: is this a direct-message channel with a user?
    # self.member: have we joined?

    def __repr__(self):
        if self.imuser:
            privflag = ' (im)'
        else:
            privflag = (' (priv)' if self.private else '')
        memberflag = (' (mem)' if self.member else '')
        return '<%s %s%s%s: "%s">' % (self.__class__.__name__, self.id, privflag, memberflag, self.name)

    def display_name(self):
        """Return the channel's printed name.
        """
        return self.name

    def muted(self):
        """Is this channel muted?
        """
        return False

    def name_parsers(self):
        """Return the matcher or matchers for this channel's name.
        (It's a list to allow for the possibility of channels named
        X/Y/Z.)
        """
        return [ NeverMatch() ]

class User:
    """Represents a user at a Host.
    """
    # self.team
    # self.id
    # self.name
    # self.real_name
    # self.client: same as self.team.client
    # self.im_channel: ID of their direct (imuser) channel (or None)
    
    def __repr__(self):
        return '<%s %s: "%s"/"%s">' % (self.__class__.__name__, self.id, self.name, self.real_name)

    def display_name(self):
        """Return the user's printed name.
        """
        return self.name

