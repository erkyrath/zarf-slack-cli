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
    await client.open()
    await client.close()

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
