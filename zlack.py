#!/usr/bin/env python

import sys
import platform
import os
import re
import time
import json
from collections import OrderedDict
import optparse
import traceback
import asyncio
import aiohttp

from zlackcli import teamdat

token_file = '.zlack-tokens'
debug_exceptions = True ###

path = os.path.join(os.environ.get('HOME'), token_file)
path = 'zh-token' ### for testing
teams = teamdat.read_teams(path)
if not teams:
    print('You are not authorized in any Slack groups. Type /auth to join one.')

async def main():
    await teamdat.create_all_sessions(teams)
    (done, pending) = await asyncio.wait([ team.load_connection_data() for team in teams.values() ])
    for res in done:
        ex = res.exception()
        if ex is not None:
            print('could not load data: %s: %s' % (ex.__class__.__name__, ex))
            if debug_exceptions:
                traceback.print_tb(ex.__traceback__)
    await teamdat.shutdown_all(teams)

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
