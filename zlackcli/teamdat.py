import sys
import os
import re
import json
from collections import OrderedDict
import traceback
import asyncio
import aiohttp
import websockets

class Team:
    """Represents one Slack group (team, workspace... I'm not all that
    consistent about it, sorry). This includes the websocket (which
    carries the RTM protocol). It also includes information about the
    group's channels and users.
    """

    # Someday we will support more than one protocol
    protocol = 'slack'
    
    def __init__(self, client, map):
        self.client = client
        self.evloop = client.evloop
        
        self.id = map['id']
        self.key = '%s:%s' % (self.protocol, self.id)
        self.team_name = map.get('team_name', '???')
        self.user_id = map['id']  # same as id
        self.access_token = map['access_token']
        self.origmap = map  # save the OrderedDict for writing out

        self.users = {}
        self.users_by_display_name = {}
        self.servers = {}
        self.channels = {}
        self.channels_by_name = {}
        self.muted_channels = set()
        self.lastchannel = None
        
        self.session = None
        self.readloop_task = None
        self.reconnect_task = None
        self.rtm_want_connected = False
        self.rtm_url = None
        self.rtm_socket = None
        self.heartbeat_interval = None

    def __repr__(self):
        return '<Team %s:%s "%s">' % (self.protocol, self.id, self.team_name)

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

    def get_aliases(self):
        """Return a list of channel aliases or None.
        """
        ls = self.client.prefs.team_get('aliases', self)
        if ls:
            return ls
        return None

    def short_name(self):
        """Return the team name or the first alias.
        """
        ls = self.client.prefs.team_get('aliases', self)
        if ls:
            return ls[0]
        return self.team_name
        
    async def open(self):
        """Create the web API session, load the team data, and open
        the RTM socket (if desired).
        """
        headers = {
            'user-agent': self.client.get_useragent(),
            'Authorization': 'Bearer '+self.access_token,
        }
        self.session = aiohttp.ClientSession(headers=headers)

        await self.load_connection_data()

        if False: ### turn off autoconnect for the moment
            await self.rtm_connect_async()

    async def close(self):
        """Shut down our session (and socket) for good.
        """
        self.want_connected = False
        if self.reconnect_task:
            self.reconnect_task.cancel()
        if self.readloop_task:
            self.readloop_task.cancel()
        ### stop heartbeat?
        if self.rtm_socket:
            await self.rtm_socket.close()
            self.rtm_socket = None
        if self.session:
            await self.session.close()
            self.session = None

    async def api_call(self, method, httpmethod='post', **kwargs):
        """Make a web API call. Return the result.
        This may raise an exception or return an object with
        ok=False.
        """
        url = 'https://{0}/api/v8/{1}'.format(self.client.domain, method)
        print('### api_call (%s) url: %s' % (httpmethod, url,))
        
        data = {}
        for (key, val) in kwargs.items():
            if val is None:
                continue
            data[key] = val
        self.client.ui.note_send_message(data, self)
        
        func = getattr(self.session, httpmethod)
        async with func(url, data=data) as resp:
            res = await resp.json()
            self.client.ui.note_receive_message(res, self)
            return res
    
    def rtm_connected(self):
        """Check whether the RTM websocket is open.
        """
        return bool(self.rtm_socket)
    
    def rtm_connect(self):
        """Open the RTM (real-time) websocket. If it's already connected,
        disconnect and reconnect.
        (Fire-and-forget call.)
        """
        task = self.evloop.create_task(self.rtm_connect_async())
        def callback(future):
            self.print_exception(future.exception(), 'RTM connect')
        task.add_done_callback(callback)
        
    async def rtm_connect_async(self, from_reconnect=False):
        """Open the RTM (real-time) websocket. If it's already connected,
        disconnect and reconnect.
        (Async call.)
        """
        if self.reconnect_task and not from_reconnect:
            self.reconnect_task.cancel()
            self.reconnect_task = None

        if self.rtm_socket:
            # Disconnect first
            await self.rtm_disconnect_async()
            await asyncio.sleep(0.05)

        self.rtm_socket = None
        self.heartbeat_interval = None
        self.want_connected = True
        self.rtm_url = self.client.prefs.get('discord_gateway_url', '')

        tries = 0
        while tries < 2 and not self.rtm_socket:
            if not self.rtm_url:
                res = await self.api_call('gateway', httpmethod='get')
                if res:
                    self.rtm_url = res.get('url')
                if not self.rtm_url:
                    self.print('gateway response had no url')
                    return
                self.print('### gateway response: ' + self.rtm_url)

            try:
                is_ssl = self.rtm_url.startswith('wss:')
                self.rtm_socket = await websockets.connect(self.rtm_url, ssl=is_ssl)
                if self.rtm_socket and not self.rtm_socket.open:
                    # This may not be a plausible failure state, but we'll cover it.
                    raise Exception('gateway did not return an open socket')
                # Success
                self.client.prefs.put('discord_gateway_url', self.rtm_url)
                
            except Exception as ex:
                self.print_exception(ex, 'Discord connection failure')
                self.rtm_socket = None
                self.rtm_url = ''
                tries += 1
                continue

        if not self.rtm_socket:
            self.print('Unable to connect to Discord gateway')
            return
        self.print('### success')

        self.readloop_task = self.evloop.create_task(self.rtm_readloop_async(self.rtm_socket))
        def callback(future):
            self.print_exception(future.exception(), 'RTM read')
        self.readloop_task.add_done_callback(callback)
        
    def rtm_disconnect(self):
        """Close the RTM (real-time) websocket.
        (Fire-and-forget call.)
        """
        task = self.evloop.create_task(self.rtm_disconnect_async())
        def callback(future):
            self.print_exception(future.exception(), 'RTM disconnect')
        task.add_done_callback(callback)
        
    async def rtm_disconnect_async(self, from_reconnect=False):
        """Close the RTM (real-time) websocket.
        (Async call.)
        """
        if self.reconnect_task and not from_reconnect:
            self.reconnect_task.cancel()
            self.reconnect_task = None
            
        if self.readloop_task:
            self.readloop_task.cancel()
            self.readloop_task = None
            
        ### stop heartbeat?
        
        self.want_connected = False
        if not self.rtm_socket:
            self.print('Team not connected: %s' % (self.team_name,))
            return
        await self.rtm_socket.close()
        self.rtm_socket = None
        if not from_reconnect:
            self.print('Disconnected from %s' % (self.team_name,))

    def handle_disconnect(self):
        """This is called whenever a ConnectionClosed error turns up
        on the websocket. We set up a task to close the socket and
        (if appropriate) try to reconnect.
        """
        if self.reconnect_task:
            self.print('Already reconnecting!')
            return
        self.reconnect_task = self.evloop.create_task(self.do_reconnect_async())
        def callback(future):
            self.reconnect_task = None
            if future.cancelled():
                return
            self.print_exception(future.exception(), 'Handle disconnect')
        self.reconnect_task.add_done_callback(callback)

    async def do_reconnect_async(self):
        """Background task to attempt reconnecting after a disconnect.
        This tries up to five times, with increasing delays, before
        giving up.
        """
        # store the want_connected value, which will be squashed by the
        # rtm_disconnect call.
        reconnect = self.want_connected
        await self.rtm_disconnect_async(True)
        if not reconnect:
            # We're manually disconnected or the client is exiting.
            return

        tries = 0
        while tries < 5:
            # Politely wait a moment before trying to reconnect. Succeeding
            # tries will use longer delays.
            delay = 1.0 + (tries * tries) * 2.0
            await asyncio.sleep(delay)
            await self.rtm_connect_async(True)
            if self.rtm_socket:
                # Successfully reconnected
                return
            # Next time, wait longer.
            tries += 1

        # We've tried five times in 60 seconds (roughly).
        self.print('Too many retries, giving up.')
        self.want_connected = False

    async def rtm_readloop_async(self, socket):
        """Begin reading messages from the RTM websocket. Continue until
        the socket closes. (Async call, obviously.)
        Each message is passed to the UI's handle_message call.
        """
        while True:
            msg = None
            try:
                msg = await socket.recv()
            except asyncio.CancelledError:
                # The read was cancelled as part of disconnect.
                return
            except websockets.ConnectionClosed:
                self.print('<ConnectionClosed: %s>' % (self.short_name(),))
                self.handle_disconnect()
                # This socket is done with; exit this loop.
                return
            except Exception as ex:
                self.print_exception(ex, 'RTM readloop')
            if not msg:
                continue
                
            obj = None
            try:
                obj = json.loads(msg)
            except Exception as ex:
                self.print_exception(ex, 'JSON decode')
                continue
            self.client.ui.note_receive_message(msg, self)
            try:
                self.client.ui.handle_message(obj, self)
            except Exception as ex:
                self.print_exception(ex, 'Message handler')
        
    def rtm_send(self, msg):
        """Send a message via the RTM websocket.
        (Fire-and-forget call.)
        """
        if not self.rtm_socket:
            self.print('Cannot send: %s not connected' % (self.team_name,))
            return
        task = self.evloop.create_task(self.rtm_send_async(msg))
        def callback(future):
            self.print_exception(future.exception(), 'RTM send')
        task.add_done_callback(callback)
        
    async def rtm_send_async(self, msg):
        """Send a message via the RTM websocket.
        (Async call.)
        """
        if not self.rtm_socket:
            self.print('Cannot send: %s not connected' % (self.team_name,))
            return
        self.client.ui.note_send_message(msg, self)
        try:
            await self.rtm_socket.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            self.print('<ConnectionClosed: %s>' % (self.short_name(),))
            self.handle_disconnect()
        except Exception as ex:
            self.print_exception(ex, 'RTM send')
        
    async def load_connection_data(self):
        """Load all the information we need for a connection: the channel
        and user lists.
        (Async call.)
        """

        self.client.print('Fetching user information for %s' % (self.team_name,))

        self.muted_channels.clear()
        self.channels.clear()
        self.channels_by_name.clear()
        self.users.clear()
        self.users_by_display_name.clear();
    
        res = await self.api_call('users/@me/guilds', httpmethod='get')
        if not isinstance(res, list):
            self.print('Unable to get Discord server list')
            return
        
        for obj in res:
            serv = DiscordServer(self, obj['id'], obj['name'])
            self.servers[serv.id] = serv

class DiscordServer:
    def __init__(self, team, id, name):
        self.team = team
        self.id = id
        self.name = name
        
    def __repr__(self):
        return '<DiscordServer %s: "%s">' % (self.id, self.name)
        
class Channel:
    """Simple object representing one channel in a group.
    """
    def __init__(self, team, id, name, private=False, member=True, im=None):
        self.team = team
        self.client = team.client
        self.id = id
        self.name = name
        self.private = private
        self.member = member
        self.imuser = im

    def __repr__(self):
        if self.imuser:
            privflag = ' (im)'
        else:
            privflag = (' (priv)' if self.private else '')
        memberflag = (' (mem)' if self.member else '')
        return '<Channel %s%s%s: "%s">' % (self.id, privflag, memberflag, self.name)

    def muted(self):
        """Check whether this channel is muted. The mute flag is stored
        in the Team, because it comes from Slack's preferences data,
        not the channel data.
        """
        return (self.id in self.team.muted_channels)
    
class User:
    """Simple object representing one user in a group.
    """
    def __init__(self, team, id, name, real_name):
        self.team = team
        self.client = team.client
        self.id = id
        self.name = name
        self.real_name = real_name
        self.im_channel = None  # May be set later
        
    def __repr__(self):
        return '<User %s: "%s"/"%s">' % (self.id, self.name, self.real_name)

def get_next_cursor(res):
    """Extract the next_cursor field from a message object. This is
    used by all Web API calls which get paginated results.
    """
    metadata = res.get('response_metadata')
    if not metadata:
        return None
    return metadata.get('next_cursor', None)
    
