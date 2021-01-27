import sys
import os
import re
import json
from collections import OrderedDict
import collections.abc
import urllib.parse
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
    protocol = 'mattermost'
    
    def __init__(self, client, map):
        self.client = client
        self.evloop = client.evloop
        
        self.id = map['host']
        self.key = '%s:%s' % (self.protocol, self.id)
        self.team_name = self.id
        self.user_id = map['user_id']
        self.access_token = map['access_token']
        self.origmap = map  # save the OrderedDict for writing out

        self.users = {}
        self.users_by_display_name = {}
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
        self.msg_counter = 0
        self.msg_in_flight = {}

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

        if True:
            await self.rtm_connect_async()

    async def close(self):
        """Shut down our session (and socket) for good.
        """
        self.want_connected = False
        if self.reconnect_task:
            self.reconnect_task.cancel()
        if self.readloop_task:
            self.readloop_task.cancel()
        if self.rtm_socket:
            await self.rtm_socket.close()
            self.rtm_socket = None
        if self.session:
            await self.session.close()
            self.session = None

    async def api_call(self, method, httpmethod='get', **kwargs):
        """Make a web API call. Return the result.
        In the kwargs, keys starting with __ become query parameters;
        the rest become body data parameters. (Sorry, it's hacky.)
        This may raise an exception or return an object with
        status_code (http error).
        """
        url = 'https://{0}/api/v4/{1}'.format(self.client.domain, method)

        queryls = []
        data = {}
        for (key, val) in kwargs.items():
            if val is None:
                continue
            if key.startswith('__'):
                key = key[2:]
                if val is True:
                    val = 'true'
                elif val is False:
                    val = 'false'
                else:
                    val = str(val)
                queryls.append('%s=%s' % (key, urllib.parse.quote(val)))
            else:
                data[key] = val

        if queryls:
            url += ('?' + '&'.join(queryls))
        self.client.ui.note_send_message(data, self)

        print('###', httpmethod, url, data)
        httpfunc = getattr(self.session, httpmethod)
        async with httpfunc(url, data=data) as resp:
            try:
                # Disable content-type check; Mattermost seems to send text/plain for errors, even JSON errors
                res = await resp.json(content_type=None)
                self.client.ui.note_receive_message(res, self)
                return res
            except json.JSONDecodeError:
                val = await resp.text()
                raise Exception('Non-JSON response: %s' % (val[:80],))
    
    async def api_call_check(self, method, **kwargs):
        """Make a web API call. Return the result.
        On error, print an error message and return None.
        """
        try:
            res = await self.api_call(method, **kwargs)
            if res is None:
                self.client.print('Slack error (%s) (%s): no result' % (method, self.short_name(),))
                return None
            if isinstance(res, collections.abc.Mapping) and res.get('status_code') and res.get('message'):
                errmsg = res.get('message', '???')
                self.client.print('Slack error (%s) (%s): %s' % (method, self.short_name(), errmsg,))
                return None
            return res
        except Exception as ex:
            self.print_exception(ex, 'Slack exception (%s) (%s)' % (method, self.short_name(),))
            return None

    def resolve_in_flight(self, val):
        """Check an id value in a reply_to. If we've sent a message
        with that value, return it (and remove it from our pool of sent
        messages.)
        """
        if val in self.msg_in_flight:
            return self.msg_in_flight.pop(val)
        return None
        
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
            
        self.want_connected = True
        self.rtm_url = 'wss://{0}/api/v4/{1}'.format(self.client.domain, 'websocket')

        is_ssl = self.rtm_url.startswith('wss:')
        self.rtm_socket = await websockets.connect(self.rtm_url, ssl=is_ssl)
        if self.rtm_socket and not self.rtm_socket.open:
            # This may not be a plausible failure state, but we'll cover it.
            self.print('websocket did not return an open socket')
            self.rtm_socket = None
            return

        await self.rtm_send_async({ 'action':'authentication_challenge', 'seq':None, 'data':{ 'token':self.access_token } })

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
            except websockets.ConnectionClosed as ex:
                self.print('<ConnectionClosed: %s (%s "%s")>' % (self.short_name(), ex.code, ex.reason,))
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
        if 'seq' in msg and msg['seq'] is None:
            self.msg_counter += 1
            msg['seq'] = self.msg_counter
            self.msg_in_flight[msg['seq']] = msg
        self.client.ui.note_send_message(msg, self)
        try:
            await self.rtm_socket.send(json.dumps(msg))
        except websockets.ConnectionClosed as ex:
            self.print('<ConnectionClosed: %s (%s "%s")>' % (self.short_name(), ex.code, ex.reason,))
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

        ### muted channels
    
        # Fetch user lists
        page = 0
        while True:
            res = await self.api_call_check('users', __page=page)
            if not res:
                break
            for user in res:
                userid = user['id']
                username = user['username']
                userrealname = (user.get('first_name') + ' ' + user.get('last_name')).strip()
                self.users[userid] = User(self, userid, username, userrealname)
                self.users_by_display_name[username] = self.users[userid]
            page += 1
            
        #self.client.print('Users for %s: %s' % (self, self.users,))
    
        ### Fetch public and private channels
            
        ### Fetch IM (person-to-person) channels

        #self.client.print('Channels for %s: %s' % (self, self.channels,))

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

