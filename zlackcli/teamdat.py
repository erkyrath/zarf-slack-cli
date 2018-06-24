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
        
        self.id = map['team_id']
        self.key = '%s:%s' % (self.protocol, self.id)
        self.team_name = map.get('team_name', '???')
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
        self.rtm_url = None
        self.rtm_socket = None
        self.msg_counter = 0
        self.msg_in_flight = {}

    def __repr__(self):
        return '<Team %s:%s "%s">' % (self.protocol, self.id, self.team_name)

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) This is normally just print(), but you could
        subclass this and customize it.
        """
        self.client.print(msg)

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        self.client.print_exception(ex, '%s (%s)' % (label, self.team_name)) ### alias?
        
    async def open(self):
        """Create the web API session, load the team data, and open
        the RTM socket (if desired)
        """
        headers = {
            'user-agent': self.client.get_useragent(),
            'Authorization': 'Bearer '+self.access_token,
        }
        self.session = aiohttp.ClientSession(headers=headers)

        await self.load_connection_data()

        if True:
            await self.rtm_connect_task()

    async def close(self):
        """Shut down our session.
        """
        if self.rtm_socket:
            await self.rtm_socket.close()
            self.rtm_socket = None
        if self.session:
            await self.session.close()
            self.session = None

    async def api_call(self, method, **kwargs):
        """Make a web API call. Return the result.
        This may raise an exception or return an object with
        ok=False.
        """
        url = 'https://{0}/api/{1}'.format(self.client.domain, method)
        
        data = {}
        for (key, val) in kwargs.items():
            if val is None:
                continue
            ### channels, users, types: convert list to comma-separated string
            ### other lists/dicts: convert to json.dumps()
            data[key] = val
            
        async with self.session.post(url, data=data) as resp:
            return await resp.json()
    
    async def api_call_check(self, method, **kwargs):
        """Make a web API call. Return the result.
        On error, print an error message and return None.
        """
        try:
            res = await self.api_call(method, **kwargs)
            if res is None or not res.get('ok'):
                self.client.print('Slack error (%s) (%s): %s' % (method, self.team_name, res.get('error', '???'),)) ### alias?
                return None
            return res
        except Exception as ex:
            self.print_exception(ex, 'Slack exception (%s)' % (method,))
            return None

    def rtm_connect(self):
        task = self.evloop.create_task(self.rtm_connect_task())
        def callback(future):
            self.print_exception(future.exception(), 'RTM connect')
        task.add_done_callback(callback)
        
    async def rtm_connect_task(self):
        if self.rtm_socket:
            # Disconnect first
            await self.rtm_disconnect_task()
            
        res = await self.api_call_check('rtm.connect')
        if not res:
            return
        self.rtm_url = res.get('url')
        if not self.rtm_url:
            self.print('rtm.connect response had no url')
            return
        
        is_ssl = self.rtm_url.startswith('wss:')
        self.rtm_socket = await websockets.connect(self.rtm_url, ssl=is_ssl)

        task = self.evloop.create_task(self.rtm_readloop_task(self.rtm_socket))
        def callback(future):
            self.print_exception(future.exception(), 'RTM read')
        task.add_done_callback(callback)

    async def rtm_readloop_task(self, socket):
        while True:
            msg = None
            try:
                msg = await socket.recv()
            except websockets.ConnectionClosed:
                print('<ConnectionClosed: %s>' % (self.team_name,))
                ### reconnect? with back-off; unless quitting
                return
            if not msg:
                continue
            obj = None
            try:
                obj = json.loads(msg)
            except Exception as ex:
                self.print_exception(ex, 'JSON decode')
                continue
            try:
                self.client.ui.handle_message(obj, self)
            except Exception as ex:
                self.print_exception(ex, 'JSON decode')
        
    def rtm_disconnect(self):
        task = self.evloop.create_task(self.rtm_disconnect_task())
        def callback(future):
            self.print_exception(future.exception(), 'RTM disconnect')
        task.add_done_callback(callback)
        
    async def rtm_disconnect_task(self):
        if not self.rtm_socket:
            self.print('Team not connected: %s' % (self.team_name,))
            return
        await self.rtm_socket.close()
        self.rtm_socket = None
        self.print('Disconnected from %s' % (self.team_name,))

    async def rtm_send(self, msg):
        if not self.connected():
            self.print('Cannot send: %s not connected' % (self.team_name,))
            return
        if 'id' in msg and msg['id'] is None:
            self.msg_counter += 1
            msg['id'] = self.msg_counter
            self.msg_in_flight[msg['id']] = msg
        #if debug_messages:
        #    thread.add_output('Sending (%s): %s' % (team_name(self.teamref), msg,))
        try:
            await self.rtm_socket.send(json.dumps(msg))
        except Exception as ex:
            self.print_exception(ex, 'RTM send')
            ### reconnect? backoff, etc
        
    async def load_connection_data(self):
        """Load all the information we need for a connection: the channel
        and user lists.
        """

        self.client.print('Fetching user information for %s' % (self.team_name,))

        self.muted_channels.clear()
        self.channels.clear()
        self.channels_by_name.clear()
        self.users.clear()
        self.users_by_display_name.clear();
    
        # The muted_channels information is stored in your Slack preferences,
        # which are an undocumented (but I guess widely used) API call.
        # See: https://github.com/ErikKalkoken/slackApiDoc
        res = await self.api_call_check('users.prefs.get')
        if res:
            prefs = res.get('prefs')
            mutels = prefs.get('muted_channels')
            if mutels:
                self.muted_channels = set(mutels.split(','))

        # Fetch user lists
        cursor = None
        while True:
            res = await self.api_call_check('users.list', cursor=cursor)
            if not res:
                break
            for user in res.get('members'):
                userid = user['id']
                username = user['profile']['display_name']
                if not username:
                    username = user['name']    # legacy data field
                userrealname = user['profile']['real_name']
                self.users[userid] = User(self, userid, username, userrealname)
                self.users_by_display_name[username] = self.users[userid]
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        self.client.print('Users for %s: %s' % (self, self.users,))
    
        # Fetch public and private channels
        cursor = None
        while True:
            res = await self.api_call_check('conversations.list', exclude_archived=True, types='public_channel,private_channel', cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                channame = chan['name']
                priv = chan['is_private']
                member = chan['is_member']
                self.channels[chanid] = Channel(self, chanid, channame, private=priv, member=member)
                self.channels_by_name[channame] = self.channels[chanid]
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        # Fetch IM (person-to-person) channels
        cursor = None
        while True:
            res = await self.api_call_check('conversations.list', exclude_archived=True, types='im', cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                chanuser = chan['user']
                if chanuser in self.users:
                    self.users[chanuser].im_channel = chanid
                    channame = '@'+self.users[chanuser].name
                    self.channels[chanid] = Channel(self, chanid, channame, private=True, member=True, im=chanuser)
                    # But not channels_by_name.
            cursor = get_next_cursor(res)
            if not cursor:
                break

        self.client.print('Channels for %s: %s' % (self, self.channels,))

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
    
