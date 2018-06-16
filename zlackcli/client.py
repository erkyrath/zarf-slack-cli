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
        print(str(msg))

    def print_exception(self, ex, label):
        if ex is None:
            return
        self.print('%s: %s: %s' % (label, ex.__class__.__name__, ex))
        if self.debug_exceptions:
            ls = traceback.format_tb(ex.__traceback__)
            for ln in ls:
                self.print(ln.rstrip())
        
    def read_teams(self):
        """Read the current token list from ~/.zlack-tokens.
        Return a dict of Team objects.
        """
        try:
            fl = open(self.tokenpath)
            dat = json.load(fl, object_pairs_hook=OrderedDict)
            fl.close()
        except:
            return
        for (id, map) in dat.items():
            self.teams[id] = Team(self, map)
    
    async def open(self):
        useragent = 'zlack Python/{v.major}.{v.minor}.{v.micro} {psys}/{pver}'.format(v=sys.version_info, psys=platform.system(), pver=platform.release()) ### should include zlack version also
        
        for team in self.teams.values():
            headers = {
                'user-agent': useragent,
                'Authorization': 'Bearer '+team.access_token,
            }
            team.session = aiohttp.ClientSession(headers=headers)

        if self.teams:
            (done, pending) = await asyncio.wait([ team.load_connection_data() for team in self.teams.values() ])
            for res in done:
                self.print_exception(res.exception(), 'could not load data')
    
    async def close(self):
        if self.authtask:
            self.authtask.cancel()
            
        for team in self.teams.values():
            if team.session:
                await team.session.close()
                team.session = None

    def begin_auth(self, evloop):
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
        (slackurl, redirecturl, statecheck) = construct_auth_url(self.opts.auth_port, self.opts.client_id)

        self.print('Visit this URL to authenticate with Slack:\n')
        self.print(slackurl+'\n')

        future = asyncio.Future(loop=evloop)
        
        self.print('### launching server...')
        server = aiohttp.web.Server(construct_auth_handler(future, statecheck))
        sockserv = await evloop.create_server(server, 'localhost', self.opts.auth_port)

        res = None
        try:
            res = await asyncio.wait_for(future, 60, loop=evloop)
        except asyncio.TimeoutError:
            self.print('URL redirect timed out.')
        except asyncio.CancelledError:
            self.print('URL redirect cancelled.')
        except Exception as ex:
            self.print_exception(ex, 'wait for URL redirect')
        self.print('### got result %s, %s' % (res, future))

        await server.shutdown()
        sockserv.close()

        if not res:
            return
        
        
    
