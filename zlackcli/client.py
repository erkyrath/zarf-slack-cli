import sys
import os
import platform
from collections import OrderedDict
import json
import asyncio
import aiohttp

from .teamdat import Team

class ZlackClient:
    
    def __init__(self, tokenpath, debug_exceptions=False):
        self.teams = OrderedDict()
        self.tokenpath = tokenpath
        self.debug_exceptions = debug_exceptions
        
        self.read_teams()
        if not self.teams:
            print('You are not authorized in any Slack groups. Type /auth to join one.')
        
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
                'Authorization': 'Bearer {}'.format(team.access_token),
            }
            team.session = aiohttp.ClientSession(headers=headers)
    
    async def close(self):
        for team in self.teams.values():
            if team.session:
                await team.session.close()
                team.session = None
    
    
        
    
