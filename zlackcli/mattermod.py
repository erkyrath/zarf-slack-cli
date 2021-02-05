import sys
import time
import os.path
import tempfile
import re
import json
from collections import OrderedDict
import collections.abc
import random
import urllib.parse
import asyncio
import aiohttp
import aiohttp.web
import websockets

from .teamdat import Protocol, ProtoUI, Host, Channel, User
from .parsematch import ParseMatch

class MattermProtocol(Protocol):
    """The Mattermost protocol.
    """
    
    key = 'mattermost'
    # hostclass is filled in at init time

    base_api_url = 'https://MHOST'
    base_auth_url = 'https://MHOST/oauth/authorize'
    
    def __init__(self, client):
        super().__init__(client)
        MattermProtocol.hostclass = MattermHost

        self.protoui = MattermUI(self)

        self.client_id = client.opts.mattermost_client_id
        self.client_secret = client.opts.mattermost_client_secret
        self.session = None

        self.authtask = None
        self.waketask = None
    
    async def open(self):
        """Open web sessions for the client, and one for each team,
        and then load the team data. (This does not open the websockets.)
        """
        headers = {
            'user-agent': self.client.get_useragent(),
        }
        self.session = aiohttp.ClientSession(headers=headers)
            
        if self.teams:
            (done, pending) = await asyncio.wait([ team.open() for team in self.teams.values() ], loop=self.client.evloop)
            for res in done:
                self.print_exception(res.exception(), 'Could not set up team')

        self.waketask = self.client.evloop.create_task(self.wakeloop_async())
    
    async def close(self):
        """Shut down all our open sessions and whatnot, in preparation
        for quitting.
        """
        if self.authtask:
            self.authtask.cancel()
            self.authtask = None
            self.client.auth_in_progress = False
            
        if self.waketask:
            self.waketask.cancel()
            self.waketask = None

        if self.teams:
            (done, pending) = await asyncio.wait([ team.close() for team in self.teams.values() ], loop=self.client.evloop)
            # Ignore exceptions.

        if self.session:
            await self.session.close()
            self.session = None
            
    async def api_call(self, method, mhost, httpmethod='post', **kwargs):
        """Make a Mattermost API call. If kwargs contains a "token"
        field, this is used; otherwise, the call is unauthenticated.
        This is only used when authenticating to a new team.
        """
        url = self.base_api_url.replace('MHOST', mhost)
        url = '{0}/{1}'.format(url, method)
        
        data = {}
        headers = {}

        for (key, val) in kwargs.items():
            if val is None:
                continue
            if key == 'token':
                headers['Authorization'] = 'Bearer '+val
                continue
            data[key] = val

        httpfunc = getattr(self.session, httpmethod)
        async with httpfunc(url, headers=headers, data=data) as resp:
            try:
                # Disable content-type check; Mattermost seems to send text/plain for errors, even JSON errors
                return await resp.json(content_type=None)
            except json.JSONDecodeError:
                val = await resp.text()
                raise Exception('Non-JSON response: %s' % (val[:80],))

    async def wakeloop_async(self):
        """This task runs in the background and watches the system clock.
        If the clock jumps more than thirty seconds, then the machine was
        put to sleep for a while and we need to reconnect all our websockets.

        (Or the user changed the clock time, in which case we don't need to
        reconnect all our websockets but we do it anyway. Oops.)

        We also ping the server(s).

        (This exists because the async websockets library isn't real
        good at announcing timeout errors. If we just wait for
        ConnectionClosed exceptions to appear, we could be staring at
        a failed socket connection for a long time -- a minute or more.
        So we proactively kick everything on any apparent sleep/wake
        cycle. The server ping should make any other timeout errors
        visible.)
        """
        curtime = time.time()
        while True:
            await asyncio.sleep(5.0)
            elapsed = time.time() - curtime
            if elapsed > 30.0:
                async def reconnect_if_connected(team):
                    if team.rtm_connected():
                        await team.rtm_connect_async()
                    
                if self.teams:
                    (done, pending) = await asyncio.wait([ reconnect_if_connected(team) for team in self.teams.values() ], loop=self.client.evloop)
                    for res in done:
                        self.print_exception(res.exception(), 'Could not reconnect team')
                
            # Server pings: not in Mattermost.

            # Note the time for next go-around. (Should be exactly five
            # seconds, but if the machine sleeps, it'll be more.)
            curtime = time.time()
            
    def begin_auth(self, mhost=None, patoken=None):
        """Launch the process of authenticating to a new Mattermost team.
        (This returns immediately.)
        """
        if self.client.auth_in_progress or self.authtask:
            self.print('Already awaiting authentication callback!')
            return

        if not mhost:
            self.print('You must give the mattermost hostname.')
            return

        if patoken is None:
            # With a patoken, the client id/secret are not required.
            if not self.client_id:
                self.print('You must set --mattermost-client-id or $MATTERMOST_CLIENT_ID to use the /auth command.')
                return
            if not self.client_secret:
                self.print('You must set --mattermost-client-secret or $MATTERMOST_CLIENT_SECRET to use the /auth command.')
                return

        if patoken is None:
            task = self.perform_oauth_async(mhost)
        else:
            task = self.perform_tokenauth_async(mhost, patoken)
        self.client.auth_in_progress = True
        self.authtask = self.client.evloop.create_task(task)
        def callback(future):
            # This is not called if authtask is cancelled. (But it is called
            # if the auth's future is cancelled.)
            self.authtask = None
            self.client.auth_in_progress = False
            self.print_exception(future.exception(), 'Begin auth')
        self.authtask.add_done_callback(callback)
        
    async def perform_oauth_async(self, mhost):
        """Do the work of authenticating to a new Mattermost team.
        This is async, and it takes a while, because the user has to
        authenticate through Mattermost's web site.
        """
        (authurl, redirecturl, statecheck) = self.construct_auth_url(mhost, self.client.opts.auth_port, self.client_id)

        self.print('Visit this URL to authenticate with Mattermost:\n')
        self.print(authurl+'\n')

        future = asyncio.Future(loop=self.client.evloop)

        # Bring up a local web server to wait for the redirect callback.
        # When we get it, the future will be set.
        server = aiohttp.web.Server(self.construct_auth_handler(future, statecheck))
        sockserv = await self.client.evloop.create_server(server, 'localhost', self.client.opts.auth_port)

        # Wait for the callback. (With a timeout.)
        auth_code = None
        try:
            auth_code = await asyncio.wait_for(future, 60, loop=self.client.evloop)
        except asyncio.TimeoutError:
            self.print('URL redirect timed out.')
        except asyncio.CancelledError:
            self.print('URL redirect cancelled.')
        except Exception as ex:
            self.print_exception(ex, 'Wait for URL redirect')

        # We're done with the local server.
        await server.shutdown()
        sockserv.close()

        if not auth_code:
            # We were cancelled or something.
            return
        
        self.print('Mattermost authentication response received.')
        
        # We have the temporary authorization code. Now we exchange it for
        # a permanent access token.

        res = await self.api_call('oauth/access_token', mhost=mhost, grant_type='authorization_code', redirect_uri=redirecturl, client_id=self.client_id, client_secret=self.client_secret, code=auth_code)
        
        if not res.get('access_token'):
            self.print('oauth/access_token response had no access_token')
            return

        expires_in = res.get('expires_in')
        refresh_token = res.get('refresh_token')
        
        # Got the permanent token.
        await self.perform_tokenauth_async(mhost, res['access_token'], expires_in=expires_in, refresh_token=refresh_token)

    async def perform_tokenauth_async(self, mhost, access_token, expires_in=None, refresh_token=None):
        """Continue the authentication process. The token may be a personal
        access token, or it may have arrived through OAuth.
        """

        # Create a new entry for ~/.zlack-tokens.
        teammap = OrderedDict()
        teammap['_protocol'] = MattermProtocol.key
        teammap['host'] = mhost
        teammap['access_token'] = access_token

        # Try fetching user info. (We want to include the user's name in the
        # ~/.zlack-tokens entry.)
        # (Note that the client-level api_call() method doesn't add the api/v4 for us.)
        res = await self.api_call('api/v4/users/me', mhost=mhost, httpmethod='get', token=teammap['access_token'])
        if not (res.get('id') and res.get('username')):
            self.print('users.info call failed: %s' % (res.get('error'),))
            return

        teammap['user_id'] = res['id']
        teammap['user_name'] = res['username']
        teammap['user_real_name'] = (res.get('first_name') + ' ' + res.get('last_name')).strip()
            
        # Create a new Team entry.
        team = self.create_team(teammap)
        self.client.write_teams()

        ### put expires_in/refresh_token in preferences?
        
        await team.open()
        
    def construct_auth_url(self, mhost, authport, clientid):
        """Construct the URL which the user will use for authentication.
        Returns (authurl, redirecturl, statestring).
        - authurl: the URL which the user should enter into a browser.
        - redirecturl: the localhost URL which Mattermost will send the user back to
          after authenticating.
        - statestring: used to double-check the user's reply when it comes
          back.
        """
        redirecturl = 'http://localhost:%d/' % (authport,)
        statecheck = 'state_%d' % (random.randrange(1000000),)

        authurl = self.base_auth_url.replace('MHOST', mhost)
        
        params = [
            ('client_id', clientid),
            ('response_type', 'code'),
            ('redirect_uri', redirecturl),
            ('state', statecheck),
        ]
        queryls = [ '%s=%s' % (key, urllib.parse.quote(val)) for (key, val) in params ]
        tup = list(urllib.parse.urlparse(authurl))
        tup[1] = mhost
        tup[4] = '&'.join(queryls)
        authurl = urllib.parse.urlunparse(tup)
        
        return (authurl, redirecturl, statecheck)

class MattermUI(ProtoUI):
    """This module translates between the UI (human-readable input and
    output) and the protocol (with its protocol-specific messages).
    """
    
    def send_message(self, text, team, chanid):
        task = self.client.evloop.create_task(self.send_message_async(text, team, chanid))
        def callback(future):
            team.print_exception(future.exception(), 'send message')
        task.add_done_callback(callback)
        
    async def send_message_async(self, text, team, chanid):
        chan = team.channels[chanid]
        etext = self.encode_message(team, text)
        await team.api_call_check('posts', httpmethod='post', channel_id=chan.realid, message=etext)
    
    def encode_message(self, team, val):
        """Encode a human-typed message into standard Mattermost form.
        """
        val = val.replace('&', '&amp;')
        val = val.replace('<', '&lt;')
        val = val.replace('>', '&gt;')
        return val
    
    def handle_message(self, msg, team):
        """Handle one message received from the Mattermost server (over the
        RTM websocket).
        """
        typ = msg.get('event')

        if typ is None and msg.get('seq_reply'):
            # A reply to a message we sent.
            origmsg = team.resolve_in_flight(msg.get('seq_reply'))
            if not origmsg:
                self.print('Mismatched reply_to (id %d, msg %s)' % (msg.get('seq_reply'), msg.get('text')))
                return
            self.print('### reply to %s' % (origmsg,))
            if False: ###
                chanid = origmsg.get('channel', '')
                userid = origmsg.get('user', '')
                # Print our successful messages even on muted channels
                ### ? not always a text message. Check format anyhow.
                text = self.decode_message(team, msg.get('text'))
                val = '[%s/%s] %s: %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), self.ui.user_name(team, userid), text)
                self.print(val)
            return
        
        if typ == 'hello':
            # Websocket-connected message.
            self.print('<Connected: %s>' % (self.ui.team_name(team)))
            return
        
        if typ == 'posted':
            data = msg.get('data', {})
            subteamid = data.get('team_id', '')
            # subteamid is empty for DM messages
            subteam = team.subteams.get(subteamid)
            try:
                post = json.loads(data.get('post', ''))
            except:
                post = {}
            userid = post.get('user_id', '')
            chanid = post.get('channel_id', '')
            if subteam:
                chanid = '%s/%s' % (subteam.name, chanid,)
            if chanid in team.muted_channels:
                return
            subtype = post.get('type', '')
            files = None
            metadata = post.get('metadata')
            if metadata:
                files = metadata.get('files')
                if files:
                    for fil in files:
                        filid = fil.get('id')
                        self.client.note_file_data(team, filid, fil)
            text = self.decode_message(team, post.get('message'), files=files)
            colon = (':' if subtype != 'me' else '')
            val = '[%s/%s] %s%s %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), self.ui.user_name(team, userid), colon, text)
            self.print(val)
            self.ui.lastchannel = (team.key, chanid)
            return

        if typ == 'post_edited' or typ == 'post_deleted':
            data = msg.get('data', {})
            try:
                post = json.loads(data.get('post', ''))
            except:
                post = {}
            userid = post.get('user_id', '')
            chan = team.channels_by_realid.get(post.get('channel_id', ''))
            chanid = chan.id if chan else ''
            if chanid in team.muted_channels:
                return
            subtype = post.get('type', '')
            files = None
            metadata = post.get('metadata')
            if metadata:
                files = metadata.get('files')
                if files:
                    for fil in files:
                        filid = fil.get('id')
                        self.client.note_file_data(team, filid, fil)
            text = self.decode_message(team, post.get('message'), files=files)
            colon = (':' if subtype != 'me' else '')
            postact = ('edit' if typ == 'post_edited' else 'del')
            val = '[%s/%s] (%s) %s%s %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), postact, self.ui.user_name(team, userid), colon, text)
            self.print(val)
            self.ui.lastchannel = (team.key, chanid)
            return
        
    def decode_message(self, team, val, files=None):
        """Convert a plain-text message in standard Mattermost form into a printable
        string.
        Mattermost message text has a few special features:
        - &, <, and > characters are &-encoded (as in HTML) (It's not clear
          why the standard browser client does this, but it does.)
        - \\ turns to \. (Ditto.)
        """
        if val is None:
            val = ''
        else:
            if '\n' in val:
                val = val.replace('\n', '\n... ')
            if '&' in val:
                val = val.replace('&lt;', '<')
                val = val.replace('&gt;', '>')
                val = val.replace('&amp;', '&')
            if '\\' in val:
                val = val.replace('\\\\', '\\')
        if files:
            for fil in files:
                fileid = fil.get('id')
                tup = self.client.files_by_id.get(fileid, None)
                index = tup[0] if tup else '?'
                val += ('\n..file [%s] %s (%s, %s bytes)' % (index, fil.get('name'), fil.get('extension'), fil.get('size'), ))
        return val
    
    async def fetch_data(self, team, fil):
        filid = fil['id']
        filename = fil.get('name')
        if not filename:
            filename = '%s.%s' % (filid, fil.get('extension', ''),)
        pathname = os.path.join(tempfile.gettempdir(), filename)

        dat = await team.api_call_data('files/%s' % (filid,))
        
        fl = open(pathname, 'wb')
        fl.write(dat)
        fl.close()
        self.print('Fetched %d bytes: %s' % (len(dat), pathname,))
        await self.display_path(pathname)
        
    async def fetch_url(self, team, url):
        tup = urllib.parse.urlparse(url)
        if not tup.netloc.lower().endswith(team.id):
            self.print('URL does not appear to be a Mattermost URL: %s' % (url,))
            return
        await super().fetch_url(team, url)
    
    
class MattermHost(Host):
    """Represents one Mattermost group (team, workspace... I'm not all that
    consistent about it, sorry). This includes the websocket (which
    carries the RTM protocol). It also includes information about the
    group's channels and users.
    """

    protocolkey = 'mattermost'
    
    def __init__(self, protocol, map):
        if not isinstance(protocol, MattermProtocol):
            raise Exception('MattermHost called with wrong protocol')
        if map['_protocol'] != self.protocolkey:
            raise Exception('MattermHost data has the wrong protocol')
        
        self.protocol = protocol
        self.client = protocol.client
        self.evloop = self.client.evloop
        
        self.id = map['host']
        self.key = '%s:%s' % (self.protocolkey, self.id)
        self.team_name = self.id
        self.user_id = map['user_id']
        self.access_token = map['access_token']
        self.origmap = map  # save the OrderedDict for writing out

        # The modularity here is wrong.
        self.nameparser = ParseMatch(self.team_name)
        self.update_name_parser()

        self.subteams = {}  # Mattermost teams
        
        self.users = {}
        self.users_by_display_name = {}
        self.channels = {}
        self.channels_by_name = {}
        self.channels_by_realid = {}
        self.muted_channels = set()
        
        # The last channel (id) we spoke on in this team. (That is, we
        # set this when ui.curchannel is set. We use this when switching
        # to a team without specifying a channel.)
        self.lastchannel = self.client.prefs.team_get('lastchannel', self)
        self.lastsubchannels = {}  # maps head to whole chanid
        val = self.client.prefs.team_get('lastsubchannels', self)
        if val:
            self.lastsubchannels = dict(val)
        
        self.session = None
        self.readloop_task = None
        self.reconnect_task = None
        self.rtm_want_connected = False
        self.rtm_url = None
        self.rtm_socket = None
        self.msg_counter = 0
        self.msg_in_flight = {}

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

        if False: ###
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

    async def api_call_data(self, method, httpmethod='get'):
        """Make a web API call. Return the result as raw data (bytes,
        rather than a json object).
        """
        url = self.protocol.base_api_url.replace('MHOST', self.id)
        url = '{0}/api/v4/{1}'.format(url, method)

        httpfunc = getattr(self.session, httpmethod)
        async with httpfunc(url) as resp:
            dat = await resp.read()
            return dat
    
    async def api_call(self, method, httpmethod='get', **kwargs):
        """Make a web API call. Return the result.
        In the kwargs, keys starting with __ become query parameters;
        the rest become body data parameters. (Sorry, it's hacky.)
        This may raise an exception or return an object with
        status_code (http error).
        """
        url = self.protocol.base_api_url.replace('MHOST', self.id)
        url = '{0}/api/v4/{1}'.format(url, method)

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
        self.client.ui.note_send_message('%s (%s): %s' % (url, httpmethod, data,), self)

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
                self.client.print('Mattermost error (%s) (%s): no result' % (method, self.short_name(),))
                return None
            if isinstance(res, collections.abc.Mapping) and res.get('status_code') and res.get('message'):
                errmsg = res.get('message', '???')
                self.client.print('Mattermost error (%s) (%s): %s' % (method, self.short_name(), errmsg,))
                return None
            return res
        except Exception as ex:
            self.print_exception(ex, 'Mattermost exception (%s) (%s)' % (method, self.short_name(),))
            return None

    def name_parser(self):
        return self.nameparser

    def set_last_channel(self, chanid):
        self.lastchannel = chanid
        self.client.prefs.team_put('lastchannel', chanid, self)
        
        head, _, tail = chanid.rpartition('/')
        if head:
            self.lastsubchannels[head] = chanid
            self.client.prefs.team_put('lastsubchannels', self.lastsubchannels, self)
        
    def get_last_channel(self, sibling=None):
        if sibling is not None:
            head, _, tail = sibling.id.rpartition('/')
            if head:
                return self.lastsubchannels.get(head)
            return None
        return self.lastchannel
        
    def resolve_in_flight(self, val):
        """Check a seq value in a reply_to. If we've sent a message
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
    
    async def rtm_connect_async(self, from_reconnect=False):
        """Open the RTM (real-time) websocket. If it's already connected,
        disconnect and reconnect.
        """
        if self.reconnect_task and not from_reconnect:
            self.reconnect_task.cancel()
            self.reconnect_task = None

        if self.rtm_socket:
            # Disconnect first
            await self.rtm_disconnect_async()
            await asyncio.sleep(0.05)
            
        self.want_connected = True
        url = self.protocol.base_api_url.replace('MHOST', self.id)
        url = url.replace('https:', 'wss:')
        self.rtm_url = '{0}/api/v4/{1}'.format(url, 'websocket')

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
        
    async def rtm_disconnect_async(self, from_reconnect=False):
        """Close the RTM (real-time) websocket.
        """
        if self.reconnect_task and not from_reconnect:
            self.reconnect_task.cancel()
            self.reconnect_task = None
            
        if self.readloop_task:
            self.readloop_task.cancel()
            self.readloop_task = None
            
        self.want_connected = False
        if not self.rtm_socket:
            self.print('MattermHost not connected: %s' % (self.team_name,))
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
                self.protocol.protoui.handle_message(obj, self)
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
        
    async def recap_channel(self, chanid, interval):
        """Recap the last interval seconds of a channel.
        """
        ui = self.client.ui
        timestamp = str(1000 * (int(time.time()) - interval))
        chan = self.channels.get(chanid)
        method = 'channels/%s/posts' % (chan.realid,)

        res = await self.api_call_check(method, __since=timestamp)
        if not res:
            return
        order = res.get('order')
        map = res.get('posts')
        if not order:
            return
        
        for msgid in reversed(order):
            post = map.get(msgid)
            if not post:
                continue
            userid = post.get('user_id', '')
            chan = self.channels_by_realid.get(post.get('channel_id', ''))
            chanid = chan.id if chan else ''
            subtype = post.get('type', '')
            files = None
            metadata = post.get('metadata')
            if metadata:
                files = metadata.get('files')
                if files:
                    for fil in files:
                        filid = fil.get('id')
                        self.client.note_file_data(self, filid, fil)
            ts = post.get('create_at')
            ts = ui.short_timestamp(int(ts/1000))
            if not post.get('message'):
                continue
            text = self.protocol.protoui.decode_message(self, post.get('message'), files=files)
            colon = (':' if subtype != 'me' else '')
            val = '[%s/%s] (%s) %s%s %s' % (ui.team_name(self), ui.channel_name(self, chanid), ts, ui.user_name(self, userid), colon, text)
            self.print(val)
        
    async def load_connection_data(self):
        """Load all the information we need for a connection: the channel
        and user lists.
        """

        self.client.print('Fetching user information for %s' % (self.team_name,))

        self.muted_channels.clear()
        self.channels.clear()
        self.channels_by_name.clear()
        self.channels_by_realid.clear()
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
                self.users[userid] = MattermUser(self, userid, username, userrealname)
                self.users_by_display_name[username] = self.users[userid]
            page += 1
            
        self.client.print('Users for %s: %s' % (self, list(self.users.values()),))

        res = await self.api_call_check('users/me/teams')
        for obj in res:
            subteamid = obj['id']
            subteamname = obj['name']
            subteamrealname = obj.get('display_name', subteamname)
            self.subteams[subteamid] = MattermSubteam(self, subteamid, subteamname, subteamrealname)
    
        self.client.print('Subteams for %s: %s' % (self, list(self.subteams.values()),))

        # Fetch member and IM channels

        realchannelids = set()

        for subteam in self.subteams.values():
            res = await self.api_call_check('users/me/teams/%s/channels' % (subteam.id,))
            for obj in res:
                chanid = obj['id']
                channame = obj['name']
                chansubteam = subteam
                # real name?
                if chanid in realchannelids:
                    continue  # duplicate
                private = False
                imuser = None
                if obj['type'] == 'P':
                    private = True
                elif obj['type'] == 'D':
                    private = True
                    # This relies on Mattermost's conventional handling of
                    # the IM channel name
                    tup = channame.split('__')
                    imuserid = tup[1] if (tup[0] == self.user_id) else tup[0]
                    imuser = self.users.get(imuserid)
                    if not imuser:
                        continue
                    channame = '@'+imuser.name
                    chansubteam = None
                    self.users[imuserid].im_channel = chanid
                chan = MattermChannel(self, chansubteam, chanid, channame, private=private, im=imuser)
                realchannelids.add(chanid)
                self.channels[chan.id] = chan
                self.channels_by_realid[chan.realid] = chan
                if imuser is None:
                    self.channels_by_name[channame] = chan

            # Fetch open channels. (We already have the ones you're a member of.)
            page = 0
            while True:
                res = await self.api_call_check('teams/%s/channels' % (subteam.id,), __page=page)
                if not res:
                    break
                for obj in res:
                    chanid = obj['id']
                    channame = obj['name']
                    chansubteam = subteam
                    # real name?
                    if chanid in realchannelids:
                        continue  # duplicate
                    if obj['type'] != 'O':
                        continue  # not public
                    chan = MattermChannel(self, chansubteam, chanid, channame, member=False)
                    realchannelids.add(chanid)
                    self.channels[chan.id] = chan
                    self.channels_by_realid[chan.realid] = chan
                    self.channels_by_name[channame] = chan
                    
                page += 1
            
        self.client.print('Channels for %s: %s' % (self, self.channels,))

class MattermSubteam:
    """Simple object representing a Mattermost team (within a host).
    We call it a "subteam" to avoid confusion.
    """
    def __init__(self, team, id, name, real_name):
        self.team = team
        self.client = team.client
        self.id = id
        self.name = name
        self.real_name = real_name
        
        self.nameparser = ParseMatch(self.name)
        self.update_name_parser()
    
    def __repr__(self):
        return '<%s %s: "%s"/"%s">' % (self.__class__.__name__, self.id, self.name, self.real_name)

    def update_name_parser(self):
        aliases = None
        aliasmap = self.client.prefs.team_get('subteam_aliases', self.team)
        if aliases:
            aliases = aliasmap.get(self.id)
        self.nameparser.update_aliases(aliases)
        
class MattermChannel(Channel):
    """Simple object representing one channel in a group.
    """
    def __init__(self, team, subteam, id, name, private=False, member=True, im=None):
        if subteam is None and im is None:
            raise Exception('only DM channels should be subteamless')
        if subteam is not None:
            fullid = '%s/%s' % (subteam.name, id,)
        else:
            fullid = id
        self.team = team
        self.subteam = subteam
        self.client = team.client
        self.id = fullid   # the client indexes channels as subteam/id (except for DM channels)
        self.realid = id   # what Mattermost thinks
        if subteam is not None:
            self.name = '%s/%s' % (subteam.name, name,)
        else:
            self.name = name
        self.private = private
        self.member = member
        self.imuser = im

        if subteam is not None:
            self.nameparselist = [ subteam.nameparser, ParseMatch(name) ]
        else:
            self.nameparselist = [ ParseMatch(name) ]
        
    def name_parsers(self):
        return self.nameparselist

    def muted(self):
        """Check whether this channel is muted. The mute flag is stored
        in the MattermHost, because it comes from Mattermost's preferences
        data, not the channel data.
        """
        return (self.id in self.team.muted_channels)
    
class MattermUser(User):
    """Simple object representing one user in a group.
    """
    def __init__(self, team, id, name, real_name):
        self.team = team
        self.client = team.client
        self.id = id
        self.name = name
        self.real_name = real_name
        self.im_channel = None  # May be set later
        
