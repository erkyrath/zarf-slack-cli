import sys
import time
import os
import re
import json
from collections import OrderedDict
import random
import urllib.parse
import asyncio
import aiohttp
import aiohttp.web
import websockets

from .teamdat import Protocol, ProtoUI, Host, Channel, User
from .parsematch import ParseMatch

class SlackProtocol(Protocol):
    """The Slack protocol.
    """
    
    key = 'slack'
    # hostclass is filled in at init time

    api_url = 'https://slack.com/api'
    auth_url = 'https://slack.com/oauth/authorize'
    
    def __init__(self, client):
        super().__init__(client)
        SlackProtocol.hostclass = SlackTeam

        self.protoui = SlackUI(self)

        self.client_id = client.opts.slack_client_id
        self.client_secret = client.opts.slack_client_secret
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
            (done, pending) = await asyncio.wait([ team.open() for team in self.teams.values() ])
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
            (done, pending) = await asyncio.wait([ team.close() for team in self.teams.values() ])
            # Ignore exceptions.

        if self.session:
            await self.session.close()
            self.session = None
            
    async def api_call(self, method, **kwargs):
        """Make a Slack API call. If kwargs contains a "token"
        field, this is used; otherwise, the call is unauthenticated.
        This is only used when authenticating to a new team.
        """
        url = '{0}/{1}'.format(self.api_url, method)
        
        data = {}
        headers = {}

        for (key, val) in kwargs.items():
            if val is None:
                continue
            if key == 'token':
                headers['Authorization'] = 'Bearer '+val
                continue
            data[key] = val

        async with self.session.post(url, headers=headers, data=data) as resp:
            return await resp.json()

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
                    (done, pending) = await asyncio.wait([ reconnect_if_connected(team) for team in self.teams.values() ])
                    for res in done:
                        self.print_exception(res.exception(), 'Could not reconnect team')
                
            # Server pings. We do this right after the time check because
            # that is a better way to avoid timeout errors. Now we've got
            # all the sockets restabilized, but timeout errors are still
            # possible; the pings will root them out.
            for team in self.teams.values():
                if team.rtm_connected():
                    await team.rtm_send_async({ 'type':'ping', 'id':None })

            # Note the time for next go-around. (Should be exactly five
            # seconds, but if the machine sleeps, it'll be more.)
            curtime = time.time()
            
    def begin_auth(self):
        """Launch the process of authenticating to a new Slack team.
        (This returns immediately.)
        """
        if self.client.auth_in_progress or self.authtask:
            self.print('Already awaiting authentication callback!')
            return

        if not self.client_id:
            self.print('You must set --slack-client-id or $SLACK_CLIENT_ID to use the /auth command.')
            return
        if not self.client_secret:
            self.print('You must set --slack-client-secret or $SLACK_CLIENT_SECRET to use the /auth command.')
            return

        self.client.auth_in_progress = True
        self.authtask = self.client.launch_coroutine(self.perform_auth_async(), 'Begin auth')
        def callback(future):
            # This is not called if authtask is cancelled. (But it is called
            # if the auth's future is cancelled.)
            self.authtask = None
            self.client.auth_in_progress = False
        self.authtask.add_done_callback(callback)
        
    async def perform_auth_async(self):
        """Do the work of authenticating to a new Slack team.
        This is async, and it takes a while, because the user has to
        authenticate through Slack's web site.
        """
        (slackurl, redirecturl, statecheck) = self.construct_auth_url(self.client.opts.auth_port, self.client_id)

        self.print('Visit this URL to authenticate with Slack:\n')
        self.print(slackurl+'\n')

        future = asyncio.Future(loop=self.client.evloop)

        # Bring up a local web server to wait for the redirect callback.
        # When we get it, the future will be set.
        server = aiohttp.web.Server(self.construct_auth_handler(future, statecheck))
        sockserv = await self.client.evloop.create_server(server, 'localhost', self.client.opts.auth_port)

        # Wait for the callback. (With a timeout.)
        auth_code = None
        try:
            auth_code = await asyncio.wait_for(future, 60)
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
        
        self.print('Slack authentication response received.')
        
        # We have the temporary authorization code. Now we exchange it for
        # a permanent access token.

        res = await self.api_call('oauth.access', client_id=self.client_id, client_secret=self.client_secret, code=auth_code)
        
        if not res.get('ok'):
            self.print('oauth.access call failed: %s' % (res.get('error'),))
            return
        if not res.get('team_id'):
            self.print('oauth.access response had no team_id')
            return
        if not res.get('access_token'):
            self.print('oauth.access response had no access_token')
            return

        # Got the permanent token. Create a new entry for ~/.zlack-tokens.
        teammap = OrderedDict()
        teammap['_protocol'] = SlackProtocol.key
        for key in ('team_id', 'team_name', 'user_id', 'scope', 'access_token'):
            if key in res:
                teammap[key] = res.get(key)

        # Try fetching user info. (We want to include the user's name in the
        # ~/.zlack-tokens entry.)
        res = await self.api_call('users.info', token=teammap['access_token'], user=teammap['user_id'])
        if not res.get('ok'):
            self.print('users.info call failed: %s' % (res.get('error'),))
            return
        if not res.get('user'):
            self.print('users.info response had no user')
            return
        user = res['user']

        teammap['user_name'] = user['name']
        teammap['user_real_name'] = user['real_name']
            
        # Create a new SlackTeam entry.
        team = self.create_team(teammap)
        self.client.write_teams()
        
        await team.open()
        
    def construct_auth_url(self, authport, clientid):
        """Construct the URL which the user will use for authentication.
        Returns (slackurl, redirecturl, statestring).
        - slackurl: the URL which the user should enter into a browser.
        - redirecturl: the localhost URL which Slack will send the user back to
          after authenticating.
        - statestring: used to double-check the user's reply when it comes
          back.
        """
        redirecturl = 'http://localhost:%d/' % (authport,)
        statecheck = 'state_%d' % (random.randrange(1000000),)
    
        params = [
            ('client_id', clientid),
            ('scope', 'client'),
            ('redirect_uri', redirecturl),
            ('state', statecheck),
        ]
        queryls = [ '%s=%s' % (key, urllib.parse.quote(val)) for (key, val) in params ]
        tup = list(urllib.parse.urlparse(self.auth_url))
        tup[4] = '&'.join(queryls)
        slackurl = urllib.parse.urlunparse(tup)
        
        return (slackurl, redirecturl, statecheck)

class SlackUI(ProtoUI):
    """This module translates between the UI (human-readable input and
    output) and the protocol (with its protocol-specific messages).
    """
    
    pat_user_id = re.compile('@([a-z0-9._]+)', flags=re.IGNORECASE)
    pat_channel_id = re.compile('#([a-z0-9_-]+)', flags=re.IGNORECASE)
    pat_encoded_user_id = re.compile('<@([a-z0-9_]+)>', flags=re.IGNORECASE)
    pat_encoded_channel_id = re.compile('<#([a-z0-9_]+)([|][a-z0-9_-]*)?>', flags=re.IGNORECASE)

    def send_message(self, text, team, chanid):
        """Send a message to the given team and channel.
        (This returns immediately.)
        """
        etext = self.encode_message(team, text)
        team.rtm_send({ 'type':'message', 'id':None, 'user':team.user_id, 'channel':chanid, 'text':etext })
    
    def encode_message(self, team, val):
        """Encode a human-typed message into standard Slack form.
        """
        val = val.replace('&', '&amp;')
        val = val.replace('<', '&lt;')
        val = val.replace('>', '&gt;')
        # We try to locate @displayname references and convert them to
        # <@USERID>.
        val = self.pat_user_id.sub(lambda match:self.encode_exact_user_id(team, match), val)
        val = self.pat_channel_id.sub(lambda match:self.encode_exact_channel_id(team, match), val)
        return val
    
    def encode_exact_user_id(self, team, match):
        """Utility function used by encode_message. Given a match object from
        pat_user_id, return a <@USERID> substitution. If the match doesn't
        exactly match a user display name, we return the original string.    
        """
        orig = match.group(0)  # '@name'
        val = match.group(1)   # 'name'
        if val not in team.users_by_display_name:
            return orig
        return '<@' + team.users_by_display_name[val].id + '>'
    
    def encode_exact_channel_id(self, team, match):
        """Utility function used by encode_message. Given a match object from
        pat_channel_id, return a <#CHANID> substitution. If the match doesn't
        exactly match a channel name, we return the original string.    
        """
        orig = match.group(0)  # '#channel'
        val = match.group(1)   # 'channel'
        if val not in team.channels_by_name:
            return orig
        return '<#' + team.channels_by_name[val].id + '>'

    def handle_message(self, msg, team):
        """Handle one message received from the Slack server (over the
        RTM websocket).
        """
        typ = msg.get('type')

        files = msg.get('files')
        if files:
            for fil in files:
                url = fil.get('url_private')
                self.client.note_file_data(team, url, fil)

        if typ is None and msg.get('reply_to'):
            # A reply to a message we sent.
            origmsg = team.resolve_in_flight(msg.get('reply_to'))
            if not origmsg:
                self.print('Mismatched reply_to (id %d, msg %s)' % (msg.get('reply_to'), msg.get('text')))
                return
            chanid = origmsg.get('channel', '')
            userid = origmsg.get('user', '')
            # Print our successful messages even on muted channels
            text = self.decode_message(team, msg.get('text'), attachments=msg.get('attachments'), files=msg.get('files'))
            val = '[%s/%s] %s: %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), self.ui.user_name(team, userid), text)
            self.print(val)
            return
        
        if typ == 'hello':
            # Websocket-connected message.
            self.print('<Connected: %s>' % (self.ui.team_name(team)))
            return
        
        if typ == 'message':
            chanid = msg.get('channel', '')
            userid = msg.get('user', '')
            subtype = msg.get('subtype', '')
            if chanid in team.muted_channels:
                return
            if subtype == 'message_deleted':
                userid = msg.get('previous_message').get('user', '')
                oldtext = msg.get('previous_message').get('text')
                oldtext = self.decode_message(team, oldtext)
                val = '[%s/%s] (del) %s: %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), self.ui.user_name(team, userid), oldtext)
                self.print(val)
                return
            if subtype == 'message_changed':
                oldtext = ''
                if 'previous_message' in msg:
                    oldtext = msg.get('previous_message').get('text')
                    oldtext = self.decode_message(team, oldtext)
                userid = msg.get('message').get('user', '')
                newtext = msg.get('message').get('text')
                newtext = self.decode_message(team, newtext, attachments=msg.get('attachments'), files=msg.get('files'))
                if oldtext == newtext:
                    # Most likely this is a change to attachments, caused by Slack creating an image preview. Ignore.
                    return
                text = oldtext + '\n -> ' + newtext
                val = '[%s/%s] (edit) %s: %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), self.ui.user_name(team, userid), text)
                self.print(val)
                self.ui.lastchannel = (team.key, chanid)
                return
            if subtype == 'slackbot_response':
                val = self.client.prefs.tree_get('slackbot_mute', team, chanid)
                if val:
                    return
            text = self.decode_message(team, msg.get('text'), attachments=msg.get('attachments'), files=msg.get('files'))
            subtypeflag = (' (%s)'%(subtype,) if subtype else '')
            colon = (':' if subtype != 'me_message' else '')
            val = '[%s/%s]%s %s%s %s' % (self.ui.team_name(team), self.ui.channel_name(team, chanid), subtypeflag, self.ui.user_name(team, userid), colon, text)
            self.print(val)
            self.ui.lastchannel = (team.key, chanid)
            return

    def decode_message(self, team, val, attachments=None, files=None):
        """Convert a plain-text message in standard Slack form into a printable
        string. You can also pass a list of attachments from the message.
        Slack message text has a few special features:
        - User references look like <@USERID>
        - URLs look like <URL> or <URL|SLUG>
        - &, <, and > characters are &-encoded (as in HTML)
        """
        if val is None:
            val = ''
        else:
            val = self.pat_encoded_user_id.sub(lambda match:'@'+self.ui.user_name(team, match.group(1)), val)
            val = self.pat_encoded_channel_id.sub(lambda match:'#'+self.ui.channel_name(team, match.group(1))+(match.group(2) if match.group(2) else ''), val)
            # We could translate <URL> and <URL|SLUG> here, but those look fine as is
            if '\n' in val:
                val = val.replace('\n', '\n... ')
            if '&' in val:
                val = val.replace('&lt;', '<')
                val = val.replace('&gt;', '>')
                val = val.replace('&amp;', '&')
        if attachments:
            for att in attachments:
                fallback = att.get('fallback')
                if fallback:
                    if '\n' in fallback:
                        fallback = fallback.replace('\n', '\n... ')
                    ### & < > also?
                    val += ('\n..> ' + fallback)
        if files:
            for fil in files:
                url = fil.get('url_private')
                tup = self.client.files_by_id.get(url, None)
                index = tup[0] if tup else '?'
                val += ('\n..file [%s] %s (%s, %s bytes): %s' % (index, fil.get('title'), fil.get('pretty_type'), fil.get('size'), url, ))
        return val

    async def fetch_data(self, team, fil):
        """Fetch data stored by note_file_data().
        """
        url = fil['url_private']
        await self.fetch_url(team, url)
        
    async def fetch_url(self, team, url):
        """Fetch the given URL, using the team's web credentials.
        Store the data in a temporary file.
        """
        tup = urllib.parse.urlparse(url)
        if not tup.netloc.lower().endswith('.slack.com'):
            self.print('URL does not appear to be a Slack URL: %s' % (url,))
            return
        await super().fetch_url(team, url)
    
    
class SlackTeam(Host):
    """Represents one Slack group (team, workspace... I'm not all that
    consistent about it, sorry). This includes the websocket (which
    carries the RTM protocol). It also includes information about the
    group's channels and users.
    """

    protocolkey = 'slack'
    
    def __init__(self, protocol, map):
        if not isinstance(protocol, SlackProtocol):
            raise Exception('SlackTeam called with wrong protocol')
        if map['_protocol'] != self.protocolkey:
            raise Exception('SlackTeam data has the wrong protocol')
        
        self.protocol = protocol
        self.client = protocol.client
        self.evloop = self.client.evloop
        
        self.id = map['team_id']   # looks like "T00ABC123"
        self.key = '%s:%s' % (self.protocolkey, self.id)
        self.team_name = map.get('team_name', '???')
        self.user_id = map['user_id']
        self.access_token = map['access_token']
        self.origmap = map  # save the OrderedDict for writing out

        # The modularity here is wrong.
        self.nameparser = ParseMatch(self.team_name)
        self.update_name_parser()

        self.users = {}
        self.users_by_display_name = {}
        self.channels = {}
        self.channels_by_name = {}
        self.muted_channels = set()
        
        # The last channel (id) we spoke on in this team. (That is, we
        # set this when ui.curchannel is set. We use this when switching
        # to a team without specifying a channel.)
        self.lastchannel = self.client.prefs.team_get('lastchannel', self)
        
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

    async def api_call(self, method, **kwargs):
        """Make a web API call. Return the result.
        This may raise an exception or return an object with
        ok=False.
        """
        url = '{0}/{1}'.format(self.protocol.api_url, method)
        
        data = {}
        for (key, val) in kwargs.items():
            if val is None:
                continue
            ### channels, users, types: convert list to comma-separated string
            ### other lists/dicts: convert to json.dumps()
            data[key] = val
        self.client.ui.note_send_message(data, self)
        
        async with self.session.post(url, data=data) as resp:
            res = await resp.json()
            self.client.ui.note_receive_message(res, self)
            return res
    
    async def api_call_check(self, method, **kwargs):
        """Make a web API call. Return the result.
        On error, print an error message and return None.
        """
        try:
            res = await self.api_call(method, **kwargs)
            if res is None or not res.get('ok'):
                errmsg = '???'
                if res and 'error' in res:
                    errmsg = res.get('error')
                self.client.print('Slack error (%s) (%s): %s' % (method, self.short_name(), errmsg,))
                return None
            return res
        except Exception as ex:
            self.print_exception(ex, 'Slack exception (%s)' % (method,))
            return None

    def name_parser(self):
        """Return a matcher for this host's name.
        """
        return self.nameparser

    def set_last_channel(self, chanid):
        """Note the last channel used for this team.
        """
        self.lastchannel = chanid
        self.client.prefs.team_put('lastchannel', chanid, self)
        
    def get_last_channel(self):
        """Get the last channel used for this team.
        """
        return self.lastchannel
        
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
        res = await self.api_call_check('rtm.connect')
        if not res:
            return
        self.rtm_url = res.get('url')
        if not self.rtm_url:
            self.print('rtm.connect response had no url')
            return

        is_ssl = self.rtm_url.startswith('wss:')
        self.rtm_socket = await websockets.connect(self.rtm_url, ssl=is_ssl)
        if self.rtm_socket and not self.rtm_socket.open:
            # This may not be a plausible failure state, but we'll cover it.
            self.print('rtm.connect did not return an open socket')
            self.rtm_socket = None
            return

        self.readloop_task = self.client.launch_coroutine(self.rtm_readloop_async(self.rtm_socket), 'RTM read')
        
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
            self.print('SlackTeam not connected: %s' % (self.team_name,))
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
        self.reconnect_task = self.client.launch_coroutine(self.do_reconnect_async(), 'Handle disconnect')
        def callback(future):
            self.reconnect_task = None
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
        self.client.launch_coroutine(self.rtm_send_async(msg), 'RTM send')
        
    async def rtm_send_async(self, msg):
        """Send a message via the RTM websocket.
        (Async call.)
        """
        if not self.rtm_socket:
            self.print('Cannot send: %s not connected' % (self.team_name,))
            return
        if 'id' in msg and msg['id'] is None:
            self.msg_counter += 1
            msg['id'] = self.msg_counter
            self.msg_in_flight[msg['id']] = msg
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
        timestamp = str(int(time.time()) - interval)
        cursor = None
        while True:
            res = await self.api_call_check('conversations.history', channel=chanid, oldest=timestamp, cursor=cursor)
            if not res:
                break
            for msg in reversed(res.get('messages')):
                userid = msg.get('user', '')
                subtype = msg.get('subtype', '')
                if subtype:
                    continue  # don't recap subtype messages
                ts = msg.get('ts')
                ts = ui.short_timestamp(ts)
                text = self.protocol.protoui.decode_message(self, msg.get('text'), attachments=msg.get('attachments'), files=msg.get('files'))
                val = '[%s/%s] (%s) %s: %s' % (ui.team_name(self), ui.channel_name(self, chanid), ts, ui.user_name(self, userid), text)
                self.print(val)
            cursor = get_next_cursor(res)
            if not cursor:
                break
        
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
                self.users[userid] = SlackUser(self, userid, username, userrealname)
                self.users_by_display_name[username] = self.users[userid]
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        #self.client.print('Users for %s: %s' % (self, self.users,))
    
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
                self.channels[chanid] = SlackChannel(self, chanid, channame, private=priv, member=member)
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
                    self.channels[chanid] = SlackChannel(self, chanid, channame, private=True, member=True, im=chanuser)
                    # But not channels_by_name.
            cursor = get_next_cursor(res)
            if not cursor:
                break

        #self.client.print('Channels for %s: %s' % (self, self.channels,))

class SlackChannel(Channel):
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

        self.nameparselist = [ ParseMatch(name) ]
        
    def name_parsers(self):
        """Return the matcher or matchers for this channel's name.
        """
        return self.nameparselist

    def muted(self):
        """Check whether this channel is muted. The mute flag is stored
        in the SlackTeam, because it comes from Slack's preferences data,
        not the channel data.
        """
        return (self.id in self.team.muted_channels)
    
class SlackUser(User):
    """Simple object representing one user in a group.
    """
    def __init__(self, team, id, name, real_name):
        self.team = team
        self.client = team.client
        self.id = id
        self.name = name
        self.real_name = real_name
        self.im_channel = None  # May be set later
        
def get_next_cursor(res):
    """Extract the next_cursor field from a message object. This is
    used by all Web API calls which get paginated results.
    """
    metadata = res.get('response_metadata')
    if not metadata:
        return None
    return metadata.get('next_cursor', None)
    
