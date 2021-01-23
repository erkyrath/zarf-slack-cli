import sys
import os
import platform
import time
import traceback
from collections import OrderedDict
import json
import asyncio
import aiohttp
import aiohttp.web

from .teamdat import Team
from .prefs import Prefs
from .auth import construct_auth_url, construct_auth_handler
from .ui import UI

class ZlackClient:
    
    domain = 'discord.com'
    version = '2.0.0'
    
    def __init__(self, tokenpath, prefspath=None, opts={}, loop=None):
        if loop is None:
            # Py3.7: should call get_running_loop() instead
            self.evloop = asyncio.get_event_loop()
        else:
            self.evloop = loop
            
        self.tokenpath = tokenpath
        self.opts = opts
        self.debug_exceptions = opts.debug_exceptions
        self.prefs = Prefs(self, prefspath)
        self.ui = UI(self)

        self.teams = OrderedDict()
        self.authtask = None
        self.waketask = None

        self.read_teams()
        if not self.teams:
            self.print('You are not authorized in any groups. Type /auth to join one.')

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) All client output funnels through this
        call.
        This is normally just print(), but you could subclass this and
        customize it.
        """
        print(str(msg))

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        if ex is None:
            return
        self.print('%s: %s: %s' % (label, ex.__class__.__name__, ex))
        if self.debug_exceptions:
            ls = traceback.format_tb(ex.__traceback__)
            for ln in ls:
                self.print(ln.rstrip())

    def get_team(self, key):
        """Fetch a team by key ("slack:T01235X") If not found,
        return None.
        """
        return self.teams.get(key)
    
    def read_teams(self):
        """Read the current token list from ~/.zlack-tokens.
        Fills out self.teams with Team objects.
        """
        try:
            fl = open(self.tokenpath)
            dat = json.load(fl, object_pairs_hook=OrderedDict)
            fl.close()
        except:
            return
        if isinstance(dat, OrderedDict):
            # This is an old-style tokens file from Zlack V1. Reform it
            # into a list, assuming all entries are Slack entries.
            dat = list(dat.values())
            for map in dat:
                map['_protocol'] = 'slack'
        for map in dat:
            if map['_protocol'] != 'discord':
                self.print('Protocol not recognized: %s' % (map['_protocol'],))
                continue
            team = Team(self, map)
            self.teams[team.key] = team

    def write_teams(self):
        """Write out the current team list to ~/.zlack-tokens.
        (Always chmods the file to 0700, for privacy.)
        """
        # We use the origmap object which we saved when loading in the Team.
        teamlist = []
        for team in self.teams.values():
            teamlist.append(team.origmap)
            
        try:
            fl = open(self.tokenpath, 'w')
            json.dump(teamlist, fl, indent=1)
            fl.write('\n')
            fl.close()
            os.chmod(self.tokenpath, 0o700)
        except Exception as ex:
            self.print_exception(ex, 'Writing tokens')
    
    async def api_call(self, method, httpmethod='post', **kwargs):
        """Make a Discord API call. If kwargs contains a "token"
        field, this is used; otherwise, the call is unauthenticated.
        This is only used when authenticating to a new team.
        """
        url = 'https://{0}/api/v8/{1}'.format(self.domain, method)
        print('### api_call (%s) url: %s' % (httpmethod, url,))
        
        data = {}
        headers = {}

        for (key, val) in kwargs.items():
            if val is None:
                continue
            if key == 'token':
                headers['Authorization'] = 'Bearer '+val
                continue
            data[key] = val

        func = getattr(self.session, httpmethod)
        async with func(url, headers=headers, data=data) as resp:
            return await resp.json()

    def get_useragent(self):
        """Construct a user-agent string for our web API requests.
        """
        useragent = 'zlack {self.version} Python/{v.major}.{v.minor}.{v.micro} {psys}/{pver}'.format(self=self, v=sys.version_info, psys=platform.system(), pver=platform.release())
        return useragent
    
    async def open(self):
        """Open web sessions for the client, and one for each team,
        and then load the team data. (This does not open the websockets.)
        """
        headers = {
            'user-agent': self.get_useragent(),
        }
        self.session = aiohttp.ClientSession(headers=headers)
            
        if self.teams:
            (done, pending) = await asyncio.wait([ team.open() for team in self.teams.values() ])
            for res in done:
                self.print_exception(res.exception(), 'Could not set up team')

        self.waketask = self.evloop.create_task(self.wakeloop_async())
    
    async def close(self):
        """Shut down all our open sessions and whatnot, in preparation
        for quitting.
        """
        if self.prefs:
            self.prefs.write_if_dirty()
            
        if self.authtask:
            self.authtask.cancel()
            
        if self.waketask:
            self.waketask.cancel()
            
        for team in self.teams.values():
            await team.close()

        if self.session:
            await self.session.close()
            self.session = None

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
            ###for team in self.teams.values():
            ###    if team.rtm_connected():
            ###        await team.rtm_send_async({ 'type':'ping', 'id':None })

            # Note the time for next go-around. (Should be exactly five
            # seconds, but if the machine sleeps, it'll be more.)
            curtime = time.time()

    def begin_auth(self):
        """Launch the process of authenticating to a new Slack team.
        (This returns immediately.)
        """
        if self.authtask:
            self.print('Already awaiting authentication callback!')
            return

        if not self.opts.client_id:
            self.print('You must set --clientid or $ZLACK_CLIENT_ID to use the /auth command.')
            return
        if not self.opts.client_secret:
            self.print('You must set --clientsecret or $ZLACK_CLIENT_SECRET to use the /auth command.')
            return
            
        self.authtask = self.evloop.create_task(self.perform_auth_async())
        def callback(future):
            # This is not called if authtask is cancelled. (But it is called
            # if the auth's future is cancelled.)
            self.authtask = None
            self.print_exception(future.exception(), 'Begin auth')
        self.authtask.add_done_callback(callback)
        
    async def perform_auth_async(self):
        """Do the work of authenticating to a new Slack team.
        This is async, and it takes a while, because the user has to
        authenticate through Slack's web site.
        """
        (slackurl, redirecturl, statecheck) = construct_auth_url(self.opts.auth_port, self.opts.client_id)

        self.print('Visit this URL to authenticate with Discord:\n')
        self.print(slackurl+'\n')

        future = asyncio.Future(loop=self.evloop)

        # Bring up a local web server to wait for the redirect callback.
        # When we get it, the future will be set.
        server = aiohttp.web.Server(construct_auth_handler(future, statecheck))
        sockserv = await self.evloop.create_server(server, 'localhost', self.opts.auth_port)

        # Wait for the callback. (With a timeout.)
        auth_code = None
        try:
            auth_code = await asyncio.wait_for(future, 60, loop=self.evloop)
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
        
        self.print('Discord authentication response received.')
        
        # We have the temporary authorization code. Now we exchange it for
        # a permanent access token.

        res = await self.api_call('oauth2/token', client_id=self.opts.client_id, client_secret=self.opts.client_secret, grant_type='authorization_code', code=auth_code, redirect_uri=redirecturl, scope='identify guilds messages.read')
        ### refresh timer using res.expires_in, res.refresh_token -- in prefs?
        
        if not res.get('access_token'):
            self.print('oauth2/token response had no access_token')
            return

        # Got the permanent token. Create a new entry for ~/.zlack-tokens.
        teammap = OrderedDict()
        teammap['_protocol'] = 'discord'
        for key in ('scope', 'access_token'):
            if key in res:
                teammap[key] = res.get(key)

        # Fetch user info too.
        res = await self.api_call('users/@me', httpmethod='get', token=teammap['access_token'])
        for key in ('id', 'username', 'discriminator'):
            if key in res:
                teammap[key] = res.get(key)

        # Discord isn't organized in teams. We'll file this "Team" under
        # "discord" plus the username (so you can log into multiple Discord
        # accounts if you really want to).
        teammap['team_name'] = 'discord_%s_%s' % (map.get('username', '???'), map.get('discriminator', ''),)

        # Create a new Team entry.
        team = Team(self, teammap)
        self.teams[team.key] = team
        self.write_teams()
        
        await team.open()
        
