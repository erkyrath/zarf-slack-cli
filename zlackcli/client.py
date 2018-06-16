import sys
import os
import platform
import traceback
from collections import OrderedDict
import json
import asyncio
import aiohttp
import aiohttp.web

from .teamdat import Team
from .auth import construct_auth_url, construct_auth_handler

class ZlackClient:
    
    domain = 'slack.com'
    
    def __init__(self, tokenpath, opts={}):
        self.tokenpath = tokenpath
        self.opts = opts
        self.debug_exceptions = opts.debug_exceptions

        self.teams = OrderedDict()
        self.authtask = None

        self.read_teams()
        if not self.teams:
            self.print('You are not authorized in any Slack groups. Type /auth to join one.')

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) This is normally just print(), but you could
        subclass this and customize it.
        """
        print(str(msg))

    def print_exception(self, ex, label):
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
        for (id, map) in dat.items():
            self.teams[id] = Team(self, map)
    
    async def api_call_unauth(self, method, **kwargs):
        """Make an unauthenticated Slack API call. This is only used
        when authenticating to a new team.
        """
        url = 'https://{0}/api/{1}'.format(self.domain, method)
        
        data = {}
        for (key, val) in kwargs.items():
            if val is None:
                continue
            data[key] = val

        # Create a temporary session, which is bad style, but we
        # only do it on special occasions.
        headers = {
            'user-agent': self.get_useragent(),
        }
        session = aiohttp.ClientSession(headers=headers)
            
        async with session.post(url, data=data) as resp:
            res = await resp.json()

        await session.close()
        return res

    def get_useragent(self):
        """Construct a user-agent string for our web API requests.
        """
        useragent = 'zlack Python/{v.major}.{v.minor}.{v.micro} {psys}/{pver}'.format(v=sys.version_info, psys=platform.system(), pver=platform.release()) ### should include zlack version also
        return useragent
    
    async def open(self):
        """Open web sessions for all the teams, and load their team data.
        (This does not open the websocket.)
        """
        for team in self.teams.values():
            headers = {
                'user-agent': self.get_useragent(),
                'Authorization': 'Bearer '+team.access_token,
            }
            team.session = aiohttp.ClientSession(headers=headers)

        if self.teams:
            (done, pending) = await asyncio.wait([ team.load_connection_data() for team in self.teams.values() ])
            for res in done:
                self.print_exception(res.exception(), 'could not load data')
    
    async def close(self):
        """Shut down all our open sessions and whatnot, in preparation
        for quitting.
        """
        if self.authtask:
            self.authtask.cancel()
            
        for team in self.teams.values():
            if team.session:
                await team.session.close()
                team.session = None

    def begin_auth(self, evloop):
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
            
        self.print('### beginning auth...')
        self.authtask = evloop.create_task(self.begin_auth_task(evloop))
        def callback(future):
            # This is not called if authtask is cancelled. (But it is called
            # if the auth's future is cancelled.)
            self.authtask = None
            self.print_exception(future.exception(), 'begin_auth_task')
            self.print('### ending auth...')
        self.authtask.add_done_callback(callback)
        
    async def begin_auth_task(self, evloop):
        """Do the work of authenticating to a new Slack teams.
        This is async.
        """
        (slackurl, redirecturl, statecheck) = construct_auth_url(self.opts.auth_port, self.opts.client_id)

        self.print('Visit this URL to authenticate with Slack:\n')
        self.print(slackurl+'\n')

        future = asyncio.Future(loop=evloop)

        # Bring up a local web server to wait for the redirect callback.
        # When we get it, the future will be set.
        server = aiohttp.web.Server(construct_auth_handler(future, statecheck))
        sockserv = await evloop.create_server(server, 'localhost', self.opts.auth_port)

        # Wait for the callback. (With a timeout.)
        auth_code = None
        try:
            auth_code = await asyncio.wait_for(future, 60, loop=evloop)
        except asyncio.TimeoutError:
            self.print('URL redirect timed out.')
        except asyncio.CancelledError:
            self.print('URL redirect cancelled.')
        except Exception as ex:
            self.print_exception(ex, 'wait for URL redirect')

        # We're done with the local server.
        await server.shutdown()
        sockserv.close()

        if not auth_code:
            # We were cancelled or something.
            return
        
        self.print('Slack authentication response received.')
        
        # We have the temporary authorization code. Now we exchange it for
        # a permanent access token.

        res = await self.api_call_unauth('oauth.access', client_id=self.opts.client_id, client_secret=self.opts.client_secret, code=auth_code)
        
        if not res.get('ok'):
            self.print('oauth.access call failed: %s' % (res.get('error'),))
            return
        if not res.get('team_id'):
            self.print('oauth.access response had no team_id')
            return
        teamid = res.get('team_id')

