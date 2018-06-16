#!/usr/bin/env python

import sys
import os
import optparse
import traceback
import asyncio

import zlackcli.client

token_file = '.zlack-tokens'
debug_exceptions = True ###

path = os.path.join(os.environ.get('HOME'), token_file)
path = './zh-token' ### for testing

client = zlackcli.client.ZlackClient(path, debug_exceptions=debug_exceptions)

async def main():
    await client.create_all_sessions()
    (done, pending) = await asyncio.wait([ team.load_connection_data() for team in client.teams.values() ])
    for res in done:
        ex = res.exception()
        if ex is not None:
            print('could not load data: %s: %s' % (ex.__class__.__name__, ex))
            if debug_exceptions:
                traceback.print_tb(ex.__traceback__)
    await client.shutdown_all()

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
