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
from .slackmod import SlackProtocol
from .prefs import Prefs
from .ui import UI

class ZlackClient:
    
    version = '3.0.0'
    
    def __init__(self, tokenpath, prefspath=None, opts={}, loop=None):
        if loop is None:
            # Py3.7: should call get_running_loop() instead
            self.evloop = asyncio.get_event_loop()
        else:
            self.evloop = loop

        self.protocols = [ SlackProtocol(self) ]
        self.protocolmap = { pro.key:pro for pro in self.protocols }
            
        self.tokenpath = tokenpath
        self.opts = opts
        self.debug_exceptions = opts.debug_exceptions
        self.prefs = Prefs(self, prefspath)
        self.ui = UI(self, opts=opts)

        self.teams = OrderedDict()
        self.auth_in_progress = False

        self.read_teams()
        if not self.teams:
            self.print('You are not authorized in any Slack groups. Type /auth to join one.')

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
        for teammap in dat:
            try:
                pro = self.protocolmap.get(teammap['_protocol'])
                if not pro:
                    self.print('Protocol not recognized: %s' % (teammap['_protocol'],))
                    continue
                team = pro.create_team(teammap)
            except Exception as ex:
                self.print_exception(ex, 'Reading tokens')

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
    
    async def open(self):
        (done, pending) = await asyncio.wait([ pro.open() for pro in self.protocols ])
        for res in done:
            self.print_exception(res.exception(), 'Could not set up protocol')
    
    async def close(self):
        if self.prefs:
            self.prefs.write_if_dirty()
            
        (done, pending) = await asyncio.wait([ pro.close() for pro in self.protocols ])
        for res in done:
            self.print_exception(res.exception(), 'Could not close down protocol')
        
    def get_useragent(self):
        """Construct a user-agent string for our web API requests.
        """
        useragent = 'zlack {self.version} Python/{v.major}.{v.minor}.{v.micro} {psys}/{pver}'.format(self=self, v=sys.version_info, psys=platform.system(), pver=platform.release())
        return useragent
    
