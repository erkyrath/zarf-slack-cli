#!/usr/bin/env python

import sys
import os
import optparse
import traceback
import asyncio
import prompt_toolkit

import zlackcli.client

token_file = '.zlack-tokens'
debug_exceptions = True ###

path = os.path.join(os.environ.get('HOME'), token_file)
path = './zh-token' ### for testing

client = zlackcli.client.ZlackClient(path, debug_exceptions=debug_exceptions)

async def main():
    await client.open()
    
    # Create a history storage object for the command-line prompt.
    history = prompt_toolkit.history.InMemoryHistory()

    done = False
    while not done:
        try:
            prompt = '> '
            #if curchannel:
            #    (teamid, chanid) = curchannel
            #    prompt = '%s/%s> ' % (team_name(teamid), channel_name(teamid, chanid))
            input = await prompt_toolkit.prompt_async(prompt, history=history, patch_stdout=True)
            input = input.rstrip()
            if input:
                if input == '/auth':
                    client.begin_auth_task(evloop)
                    continue
                print('Got: "' + input + '"')
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            done = True
        except EOFError:
            print('<EOFError>')
            done = True
            
    await client.close()

evloop = asyncio.get_event_loop()
evloop.run_until_complete(main())
