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

class ZlackClient:
    
    def __init__(self, tokenpath, debug_exceptions=False):
        self.teams = OrderedDict()
        self.tokenpath = tokenpath
        self.debug_exceptions = debug_exceptions

        self.read_teams()
        if not self.teams:
            self.print('You are not authorized in any Slack groups. Type /auth to join one.')

    def print(self, msg):
        print(str(msg))
        
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
                ex = res.exception()
                if ex is not None:
                    self.print('could not load data: %s: %s' % (ex.__class__.__name__, ex))
                    if self.debug_exceptions:
                        ls = traceback.format_tb(ex.__traceback__)
                        for ln in ls:
                            self.print(ln.rstrip())
    
    async def close(self):
        for team in self.teams.values():
            if team.session:
                await team.session.close()
                team.session = None
    
    async def begin_auth(self, evloop):
        self.print('### beginning auth...')

        async def handler(request):
            self.print('### got request %s' % (request,))
            return aiohttp.web.Response(text="Hello, world")
        
        server = aiohttp.web.Server(handler)
        sockserv = await evloop.create_server(server, 'localhost', 8080)
        await asyncio.sleep(5) ### wait for future or timeout

        await server.shutdown()
        sockserv.close()
        
        
        self.print('### ending auth...')
        
        
    
